"""The full-turn guidance harness's pure parts (RM16, RM45; section 7.7)."""

import argparse
import json
import os
from pathlib import Path

import pytest

from helpers import load_script_module

harness = load_script_module("guidance_turn_benchmark")

SKILLS = {"s:a": "skill-a", "s:b": "skill-b", "s:c": "skill-c"}


def case(cid, lane="en_to_en", acceptable=("s:a",), kind="positive"):
    return dict(
        id=cid,
        lane=lane,
        reference_class=kind,
        acceptable_asset_ids=list(acceptable),
        task_text=f"task {cid}",
    )


def test_the_sample_is_fixed_by_the_seed_and_balanced_by_lane():
    cases = [case(f"e{i}") for i in range(30)] + [case(f"z{i}", "zh_hans_to_en") for i in range(30)]
    first = harness.sample_cases(
        cases, SKILLS, per_lane=5, seed=7, lanes=("en_to_en", "zh_hans_to_en")
    )
    second = harness.sample_cases(
        cases, SKILLS, per_lane=5, seed=7, lanes=("en_to_en", "zh_hans_to_en")
    )
    assert [c["id"] for c in first] == [c["id"] for c in second]
    assert sum(c["lane"] == "en_to_en" for c in first) == 5
    assert sum(c["lane"] == "zh_hans_to_en" for c in first) == 5


def test_the_sample_keeps_only_positive_cases_with_an_installed_target():
    cases = [case("ok"), case("tool", acceptable=("t:x",)), case("neg", kind="negative")]
    picked = harness.sample_cases(cases, SKILLS, per_lane=5, seed=1, lanes=("en_to_en",))
    assert [c["id"] for c in picked] == ["ok"]


def row(calls, usage=None, first_system="", first_user="u"):
    return dict(
        status=200,
        request=dict(
            messages=[
                dict(role="system", content=first_system),
                dict(role="user", content=first_user),
                *(
                    dict(role="tool", tool_call_id=c["id"], content="done")
                    for c in calls
                    if c.get("id")
                ),
            ]
        ),
        calls=calls,
        usage=usage or dict(prompt_tokens=10, completion_tokens=1),
    )


def call(tool, **arguments):
    return dict(id=f"id-{tool}", name=tool, arguments=json.dumps(arguments))


def test_grading_counts_skills_read_and_a_plain_read_of_skill_md():
    rows = [
        row([call("skills_search", query="x")]),
        row([call("skills_read", name="skill-b")]),
        row([call("read", path="skills/skill-a/SKILL.md")]),
    ]
    out = harness.grade(rows, {"skill-a"})
    assert out["loaded"] == ["skill-b", "skill-a"]
    assert out["correct"] is True
    assert out["searched"] == 1
    assert out["model_calls"] == 3
    assert out["first_input"] == 10
    assert out["total_input"] == 30


def test_grading_a_run_that_loaded_the_wrong_skill():
    out = harness.grade([row([call("skills_read", name="skill-b")])], {"skill-a"})
    assert (out["correct"], out["any_skill"]) == (False, True)


def test_grading_a_run_with_no_model_call_is_not_correct():
    out = harness.grade([], {"skill-a"})
    assert (out["correct"], out["model_calls"], out["first_input"]) == (False, 0, None)


def test_grading_sees_the_setup_that_really_applied():
    system = "<available_skills><name>a</name><name>b</name></available_skills>"
    first_user = "[RelayMind guidance, not from the user] Skills that look relevant"
    out = harness.grade([row([], first_system=system, first_user=first_user)], set())
    assert (out["listed"], out["hint_seen"]) == (2, True)


def test_a_name_outside_the_skills_block_is_not_counted_as_listed():
    system = "Write <name> here. <available_skills><name>a</name></available_skills> <name>"
    assert harness.grade([row([], first_system=system)], set())["listed"] == 1


def test_every_arm_gets_read_only_tools_and_the_same_load_limit():
    for arm in harness.ARMS.values():
        config = harness.openclaw_config(
            arm, "http://127.0.0.1:1/run/x/v1", "/p", "http://127.0.0.1:2/v1"
        )
        assert config["tools"]["allow"] == ["read", "skills_read", "skills_search"]
        assert config["skills"]["limits"]["maxSkillsLoadedPerSource"] == 1000


def test_the_default_arm_has_no_plugin_and_a_guidance_arm_empties_the_list():
    default = harness.openclaw_config(harness.ARMS["default"], "http://u/v1", "/p", None)
    assert "plugins" not in default
    assert "maxSkillsInPrompt" not in default["skills"]["limits"]
    guided = harness.openclaw_config(harness.ARMS["word"], "http://u/v1", "/p", "http://g/v1")
    assert guided["skills"]["limits"]["maxSkillsInPrompt"] == 0
    entry = guided["plugins"]["entries"]["relaymind-guidance"]
    assert entry["hooks"]["allowConversationAccess"] is True
    assert entry["config"]["baseUrl"] == "http://g/v1"


@pytest.mark.parametrize(
    "arm,word",
    [
        ("word", 'keywords = "none"'),
        ("local", 'keywords = "local"'),
        ("local_external", 'picker = "external"'),
    ],
)
def test_the_gateway_file_declares_the_arm(tmp_path, arm, word):
    text = harness.gateway_toml(
        harness.ARMS[arm], tmp_path, port=1, local=("m", "a", "http://127.0.0.1:2")
    )
    assert "[guidance]\nenabled = true" in text
    assert word in text


def test_a_preflight_refuses_a_setup_that_did_not_apply():
    harness.check_applied(harness.ARMS["default"], dict(listed=40, hint_seen=False, model_calls=2))
    harness.check_applied(harness.ARMS["word"], dict(listed=0, hint_seen=True, model_calls=2))
    with pytest.raises(RuntimeError):
        harness.check_applied(harness.ARMS["word"], dict(listed=72, hint_seen=False, model_calls=2))
    with pytest.raises(RuntimeError):
        harness.check_applied(
            harness.ARMS["default"], dict(listed=0, hint_seen=False, model_calls=2)
        )
    with pytest.raises(RuntimeError):
        harness.check_applied(
            harness.ARMS["default"], dict(listed=40, hint_seen=False, model_calls=0)
        )


def test_the_summary_keeps_every_run_and_reports_its_coverage():
    runs = [
        dict(
            arm="a",
            lane="l",
            correct=True,
            any_skill=True,
            first_input=10,
            total_input=30,
            exit=0,
            hint_seen=False,
            listed=5,
            searched=0,
            wall_s=1.0,
        ),
        dict(
            arm="a",
            lane="l",
            correct=False,
            any_skill=False,
            first_input=None,
            total_input=0,
            exit=1,
            hint_seen=False,
            listed=0,
            searched=0,
            wall_s=2.0,
        ),
    ]
    summary = harness.summarize(runs)["a"]
    assert summary["runs"] == 2
    assert summary["right_skill"] == 1
    assert summary["failed_runs"] == 1
    assert summary["median_first_input"] == 10
    assert summary["by_lane"]["l"] == dict(right=1, runs=2)


def tool_result(call_id, text):
    return dict(role="tool", tool_call_id=call_id, content=text)


def test_a_skill_load_the_host_refused_does_not_count_as_loaded():
    refused = dict(name="skills_read", arguments=json.dumps(dict(name="skill-a")), id="c1")
    rows = [row([refused])]
    rows.append(row([]))
    rows[1]["request"]["messages"].append(
        tool_result("c1", 'Skill "skill-a" is not available to this agent.')
    )
    out = harness.grade(rows, {"skill-a"})
    assert out["loaded"] == []
    assert out["correct"] is False


def test_a_skill_load_with_a_result_counts_as_loaded():
    good = dict(name="skills_read", arguments=json.dumps(dict(name="skill-a")), id="c1")
    rows = [row([good]), row([])]
    rows[1]["request"]["messages"].append(tool_result("c1", "# Skill body"))
    assert harness.grade(rows, {"skill-a"})["correct"] is True


def test_grading_keeps_the_hinted_names_apart_from_what_the_model_loaded():
    hint = (
        f"{harness.HINT} Skills that look relevant to this request: skill-a, skill.b. "
        "Load one with skills_read if it helps; skills_search can find others.\n\nthe prompt"
    )
    out = harness.grade([row([call("skills_read", name="skill-c")], first_user=hint)], {"skill-a"})
    assert out["hinted"] == ["skill-a", "skill.b"]
    assert out["hint_right"] is True
    assert out["correct"] is False


def test_grading_without_a_hint_has_no_hinted_names():
    out = harness.grade([row([], first_user="the prompt")], {"skill-a"})
    assert out["hinted"] == []
    assert out["hint_right"] is False


def test_the_preflight_also_tries_a_prompt_that_only_a_description_can_match():
    # An English prompt can match a skill name, so it passes even when no description reaches the
    # gateway. A Chinese prompt against Chinese skills cannot match their Latin names.
    sample = [case("e1"), case("z1", "zh_hans_to_en"), case("zz1", "zh_hans_to_zh_hans")]
    assert [c["id"] for c in harness.preflight_cases(sample)] == ["e1", "zz1"]
    assert [c["id"] for c in harness.preflight_cases(sample[:2])] == ["e1"]
    assert [c["id"] for c in harness.preflight_cases([sample[2]])] == ["zz1"]


def test_the_summary_counts_runs_whose_hint_held_the_right_skill():
    base = dict(
        arm="word",
        lane="en_to_en",
        exit=0,
        correct=False,
        any_skill=True,
        searched=0,
        first_input=10,
        total_input=10,
        wall_s=1.0,
        hint_seen=True,
    )
    runs = [dict(base, hint_right=True), dict(base, hint_right=False)]
    assert harness.summarize(runs)["word"]["hint_right"] == 1


# --- spec 0.69: similar skills, tools and the strict limit ------------------------------------
def test_strict_arms_set_the_limit_only_when_asked():
    config = harness.openclaw_config(harness.ARMS["local_strict"], "http://p", "m", "http://g")
    entry = config["plugins"]["entries"]["relaymind-guidance"]["config"]
    assert entry["limit"] == "strict"
    config = harness.openclaw_config(harness.ARMS["local"], "http://p", "m", "http://g")
    assert "limit" not in config["plugins"]["entries"]["relaymind-guidance"]["config"]


def test_a_refused_call_is_counted_and_is_not_a_load():
    rows = [
        dict(
            request=dict(
                messages=[
                    dict(
                        role="tool",
                        tool_call_id="c1",
                        content="RelayMind limit: skill x is not selected for this request.",
                    )
                ]
            ),
            calls=[dict(id="c1", name="skills_read", arguments='{"name":"x"}')],
            usage=None,
            status=200,
        )
    ]
    outcome = harness.grade(rows, {"x"})
    assert outcome["loaded"] == []
    assert outcome["refused"] == 1
    assert outcome["correct"] is False


def test_a_tool_called_through_the_wrapper_counts_as_right():
    rows = [
        dict(
            request=dict(messages=[dict(role="tool", tool_call_id="c1", content="called")]),
            calls=[
                dict(
                    id="c1",
                    name="tool_call",
                    arguments='{"id":"gh__search_issues","args":{"q":"x"}}',
                )
            ],
            usage=None,
            status=200,
        )
    ]
    outcome = harness.grade(rows, {"search_issues"})
    assert outcome["called"] == ["search_issues"]
    assert outcome["correct"] is True


def test_a_skill_loaded_through_the_wrapper_counts_as_loaded():
    rows = [
        dict(
            request=dict(messages=[dict(role="tool", tool_call_id="c1", content="body")]),
            calls=[
                dict(
                    id="c1",
                    name="tool_call",
                    arguments='{"id":"skills_read","args":{"name":"x"}}',
                )
            ],
            usage=None,
            status=200,
        )
    ]
    assert harness.grade(rows, {"x"})["loaded"] == ["x"]


def test_each_hint_form_gives_its_names():
    for line in (
        "The skill that fits this request: a. Load it",
        "Similar skills fit this request: a, b. If the",
        "Skills that look relevant to this request: a, b. Load one",
    ):
        assert harness.hinted_names(f"{harness.HINT}\n{line}")[0] == "a"
    text = f"{harness.HINT}\nTools that look relevant: gh__x. Find them"
    assert harness.hinted_names(text) == ["x"]


def test_similar_mode_installs_every_skill_and_makes_a_case_per_language(tmp_path):
    names, cases = harness.install_similar("development", tmp_path)
    assert len(names) == 32
    assert (tmp_path / names["similar:skyline-deploy"] / "SKILL.md").exists()
    assert {"similar_en", "similar_zh"} <= {c["lane"] for c in cases}
    assert all(c["reference_class"] == "positive" for c in cases)


def test_a_similar_skill_file_is_safe_yaml_for_any_description(tmp_path):
    names, _ = harness.install_similar("development", tmp_path)
    text = (tmp_path / names["similar:skyline-deploy"] / "SKILL.md").read_text("utf-8")
    assert text.startswith('---\nname: skyline-deploy\ndescription: "')


def test_tool_mode_writes_a_server_and_list_that_only_answers(tmp_path):
    catalog = dict(
        assets=[
            dict(
                record=dict(
                    asset_id="github:x",
                    kind="mcp_tool",
                    description="Do x",
                    identity=dict(tool_name="x"),
                )
            ),
            dict(record=dict(asset_id="s", kind="skill", description="Skill", identity={})),
        ]
    )
    names, tools = harness.install_tools(catalog, tmp_path)
    assert names == {"github:x": "x"}
    listed = json.loads(tools["file"].read_text())
    assert listed == [dict(name="gh__x", description="Do x")]
    assert "tools/call" in tools["server"].read_text()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (dict(tools=True, catalog_dir=None), "--tools needs"),
        (dict(cases=None), "give --similar"),
        (dict(arms="nope"), "no arm"),
        (dict(arms="third_party", third_party_url=None), "--third-party-url"),
        (dict(arms="local", local_manifest=None), "--local-manifest"),
    ],
)
def test_a_missing_input_is_found_before_any_spending(change, message, monkeypatch):
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "unit")
    base = dict(
        tools=False,
        catalog_dir="c",
        cases="d",
        similar=None,
        arms="default",
        third_party_url="u",
        local_manifest="m",
        local_artifact="a",
    )
    assert harness.setup_problem(argparse.Namespace(**base)) is None
    assert message in harness.setup_problem(argparse.Namespace(**{**base, **change}))


def test_a_call_without_a_result_is_not_counted_as_done():
    rows = [
        dict(
            request=dict(messages=[]),
            calls=[
                dict(id="c1", name="tool_call", arguments='{"id":"gh__x","args":{}}'),
                dict(id="c2", name="skills_read", arguments='{"name":"y"}'),
            ],
            usage=None,
            status=200,
        )
    ]
    outcome = harness.grade(rows, {"x", "y"})
    assert outcome["called"] == []
    assert outcome["loaded"] == []
    assert outcome["correct"] is False


def test_a_call_is_matched_to_its_result_after_the_host_rewrites_the_id():
    rows = [
        dict(
            status=200,
            request=dict(messages=[]),
            calls=[dict(id="call_1e9f", name="skills_read", arguments='{"name":"x"}')],
            usage=None,
        ),
        dict(
            status=200,
            request=dict(messages=[dict(role="tool", tool_call_id="call1e9f", content="body")]),
            calls=[],
            usage=None,
        ),
    ]
    outcome = harness.grade(rows, {"x"})
    assert outcome["loaded"] == ["x"]
    assert outcome["correct"] is True
    assert outcome["unmatched"] == 0


def test_a_rewritten_id_still_shows_a_refusal():
    rows = [
        dict(
            status=200,
            request=dict(messages=[]),
            calls=[dict(id="call_1e9f", name="skills_read", arguments='{"name":"x"}')],
            usage=None,
        ),
        dict(
            status=200,
            request=dict(
                messages=[dict(role="tool", tool_call_id="call1e9f", content="RelayMind limit: no")]
            ),
            calls=[],
            usage=None,
        ),
    ]
    outcome = harness.grade(rows, {"x"})
    assert outcome["loaded"] == []
    assert outcome["refused"] == 1


def test_a_preflight_refuses_a_grader_that_cannot_match_calls_to_results():
    rows = [
        dict(
            status=200,
            request=dict(messages=[]),
            calls=[dict(id="a", name="skills_read", arguments='{"name":"x"}')],
            usage=None,
        ),
        dict(
            status=200,
            request=dict(messages=[dict(role="tool", tool_call_id="zzz", content="body")]),
            calls=[],
            usage=None,
        ),
    ]
    outcome = harness.grade(rows, {"x"})
    assert outcome["unmatched"] == 1
    with pytest.raises(RuntimeError, match="match"):
        harness.check_applied(harness.ARMS["default"], dict(outcome, listed=40))


def test_a_call_in_the_last_exchange_may_rightly_have_no_result():
    rows = [
        dict(
            status=200,
            request=dict(messages=[]),
            calls=[dict(id="a", name="skills_read", arguments='{"name":"x"}')],
            usage=None,
        )
    ]
    assert harness.grade(rows, {"x"})["unmatched"] == 0


def test_a_gateway_that_could_not_bind_is_a_failed_start():
    text = "Serving on http://127.0.0.1:1\nERROR: could not bind: address already in use"
    assert harness.gateway_start_failed(text) is True
    assert (
        harness.gateway_start_failed("Serving on http://127.0.0.1:1\nApplication token: x") is False
    )


def test_the_host_can_load_every_installed_skill_in_each_arm():
    for arm in harness.ARMS.values():
        limits = harness.openclaw_config(arm, "http://p", "m", "http://g")["skills"]["limits"]
        assert limits["maxSkillsLoadedPerSource"] >= 1000
        assert limits["maxCandidatesPerRoot"] >= 1000


def test_tool_mode_allows_the_fake_servers_tools_and_nothing_with_side_effects():
    tools = dict(server=Path("/s.mjs"), file=Path("/t.json"))
    allow = harness.openclaw_config(harness.ARMS["default"], "http://p", "m", None, tools)["tools"]
    assert f"{harness.MCP_SERVER}__*" in allow["allow"]
    assert not {"exec", "write", "apply_patch"} & set(allow["allow"])


@pytest.mark.parametrize(
    ("tool", "selector"),
    [
        ("tool_call", "gh__x"),
        ("tool_call", "mcp:gh:gh__x"),
        ("tool_describe", "gh__x"),
        ("tool_describe", "mcp:gh:gh__x"),
    ],
)
def test_describing_or_calling_a_tool_by_either_id_form_counts_as_choosing_it(tool, selector):
    rows = [
        dict(
            status=200,
            request=dict(messages=[dict(role="tool", tool_call_id="c1", content="ok")]),
            calls=[dict(id="c1", name=tool, arguments=json.dumps(dict(id=selector, args={})))],
            usage=None,
        )
    ]
    outcome = harness.grade(rows, {"x"})
    assert outcome["called"] == ["x"]
    assert outcome["correct"] is True


def test_a_turn_reports_how_much_of_the_needed_set_it_loaded():
    rows = [row([call("skills_read", name="a")])]
    outcome = harness.grade(rows, {"a", "b"})
    assert (outcome["found"], outcome["needed"]) == (1, 2)


def test_similar_mode_adds_complex_cases_that_need_several_skills(tmp_path):
    names, cases = harness.install_similar("development", tmp_path)
    complex_cases = [c for c in cases if c["lane"] == "similar_complex_en"]
    assert complex_cases
    assert all(len(c["acceptable_asset_ids"]) >= 3 for c in complex_cases)
    assert all(a in names for c in complex_cases for a in c["acceptable_asset_ids"])


@pytest.mark.parametrize("key", ["id", "toolId", "name"])
def test_the_grader_reads_a_tool_call_selector_the_way_the_host_does(key):
    rows = [
        dict(
            status=200,
            request=dict(messages=[dict(role="tool", tool_call_id="c1", content="called")]),
            calls=[
                dict(id="c1", name="tool_call", arguments=json.dumps({key: "gh__x", "args": {}}))
            ],
            usage=None,
        )
    ]
    assert harness.grade(rows, {"x"})["called"] == ["x"]


def test_a_call_the_host_refused_as_invalid_is_not_counted():
    rows = [
        dict(
            status=200,
            request=dict(
                messages=[
                    dict(
                        role="tool",
                        tool_call_id="c1",
                        content='Validation failed for tool "tool_call": id is required',
                    )
                ]
            ),
            calls=[dict(id="c1", name="tool_call", arguments='{"name":"gh__x"}')],
            usage=None,
        )
    ]
    outcome = harness.grade(rows, {"x"})
    assert outcome["called"] == []
    assert outcome["refused"] == 1


def test_the_grader_reads_a_skill_file_path_written_with_backslashes():
    rows = [row([call("read", path="skills\\\\skill-a\\\\SKILL.md")])]
    assert harness.grade(rows, {"skill-a"})["loaded"] == ["skill-a"]


@pytest.mark.parametrize(
    "arguments",
    [
        dict(file_path="skills/skill-a/SKILL.md"),
        dict(path="skills/skill-a/skill.md"),
        dict(path="skills/skill-a/SKILL.MD"),
    ],
)
def test_the_grader_reads_every_form_of_a_skill_file_read(arguments):
    rows = [row([call("read", **arguments)])]
    assert harness.grade(rows, {"skill-a"})["loaded"] == ["skill-a"]


def test_the_gateway_log_is_read_as_utf8(tmp_path, monkeypatch):
    import inspect

    source = inspect.getsource(harness.start_gateway)
    assert source.count('read_text(encoding="utf-8")') >= 2


def test_a_skill_whose_text_says_blocked_still_counts_as_loaded():
    """Review of PR 285: a refusal is the host's or plugin's own message, not a word in a skill."""
    body = '---\nname: skill-a\n---\nRetry blocked requests. Example: {"status": "error"}\n'
    rows = [
        dict(
            status=200,
            request=dict(messages=[dict(role="tool", tool_call_id="c1", content=body)]),
            calls=[dict(id="c1", name="skills_read", arguments='{"name":"skill-a"}')],
            usage=None,
        )
    ]
    outcome = harness.grade(rows, {"skill-a"})
    assert outcome["loaded"] == ["skill-a"]
    assert outcome["refused"] == 0


@pytest.mark.parametrize(
    "answer",
    [
        "RelayMind limit: skill x is not selected for this request.",
        '{\n  "status": "error",\n  "tool": "read",\n  "error": "Path escapes sandbox root"\n}',
        'Validation failed for tool "tool_call":\n  - id: must have required properties id',
        "Skill x is not available to this agent.",
    ],
)
def test_each_real_refusal_envelope_still_counts_as_a_refusal(answer):
    rows = [
        dict(
            status=200,
            request=dict(messages=[dict(role="tool", tool_call_id="c1", content=answer)]),
            calls=[dict(id="c1", name="skills_read", arguments='{"name":"x"}')],
            usage=None,
        )
    ]
    outcome = harness.grade(rows, {"x"})
    assert (outcome["loaded"], outcome["refused"]) == ([], 1)


def test_a_run_whose_last_model_call_failed_is_flagged_and_left_out_of_the_totals():
    ok = row([call("skills_read", name="skill-a")])
    failed = dict(row([]), status=503, usage=None)
    outcome = harness.grade([ok, failed], {"skill-a"})
    assert outcome["upstream_failed"] is True
    good = dict(outcome, arm="a", lane="l", exit=0, wall_s=1.0, upstream_failed=False)
    bad = dict(outcome, arm="a", lane="l", exit=0, wall_s=1.0, upstream_failed=True, correct=False)
    summary = harness.summarize([good, bad])["a"]
    assert summary["runs"] == 1
    assert summary["upstream_failed"] == 1


def test_a_preflight_refuses_an_arm_whose_model_calls_fail():
    outcome = dict(listed=40, hint_seen=False, model_calls=2, upstream_failed=True)
    with pytest.raises(RuntimeError, match="model"):
        harness.check_applied(harness.ARMS["default"], outcome)


def test_every_file_the_harness_reads_or_writes_is_utf8():
    """Review of PR 285: Chinese prompts must not depend on the machine's default encoding.

    Reads the harness as a syntax tree, so a call nested inside others is checked too."""
    import ast
    import inspect

    def has_encoding(call, positional_at):
        if any(k.arg == "encoding" for k in call.keywords):
            return True
        return len(call.args) > positional_at and isinstance(call.args[positional_at], ast.Constant)

    missing = []
    for node in ast.walk(ast.parse(inspect.getsource(harness))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == "read_text" and not has_encoding(node, 0):
            missing.append(ast.unparse(node)[:60])
        elif name == "write_text" and not has_encoding(node, 1):
            missing.append(ast.unparse(node)[:60])
        elif name == "open" and not any(k.arg == "encoding" for k in node.keywords):
            missing.append(ast.unparse(node)[:60])
        elif name == "run" and any(
            k.arg == "text" and getattr(k.value, "value", False) for k in node.keywords
        ):
            if not any(k.arg == "encoding" for k in node.keywords):
                missing.append(ast.unparse(node)[:60])
    assert missing == []


def test_a_summary_of_an_arm_whose_every_run_failed_upstream_does_not_crash():
    bad = dict(
        arm="a",
        lane="l",
        exit=0,
        wall_s=1.0,
        upstream_failed=True,
        correct=False,
        first_input=None,
        total_input=0,
        any_skill=False,
        searched=0,
        hint_seen=False,
    )
    summary = harness.summarize([bad])["a"]
    assert summary["runs"] == 0
    assert summary["upstream_failed"] == 1
    assert summary["median_total_input"] is None


def test_a_wrapper_call_with_input_instead_of_args_still_counts():
    rows = [
        dict(
            status=200,
            request=dict(messages=[dict(role="tool", tool_call_id="c1", content="body")]),
            calls=[
                dict(
                    id="c1", name="tool_call", arguments='{"id":"skills_read","input":{"name":"x"}}'
                )
            ],
            usage=None,
        )
    ]
    assert harness.grade(rows, {"x"})["loaded"] == ["x"]


def test_a_wrapper_call_with_dotted_args_still_counts():
    rows = [
        dict(
            status=200,
            request=dict(messages=[dict(role="tool", tool_call_id="c1", content="body")]),
            calls=[
                dict(id="c1", name="tool_call", arguments='{"id":"skills_read","args.name":"x"}')
            ],
            usage=None,
        )
    ]
    assert harness.grade(rows, {"x"})["loaded"] == ["x"]


def test_the_gateway_log_is_closed_in_the_harness_once_the_child_has_it(tmp_path, monkeypatch):
    seen = {}

    class Exited:
        def __init__(self, *args, stdout=None, **kwargs):
            seen["log"] = stdout

        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(harness.subprocess, "Popen", Exited)
    # Stopping sends real signals to a process group, so a stand-in must never reach it.
    monkeypatch.setattr(harness, "stop_gateway", lambda process: None)
    toml = tmp_path / "g.toml"
    toml.write_text("", "utf-8")
    with pytest.raises(RuntimeError):
        harness.start_gateway(toml, {})
    assert seen["log"].closed


def test_a_wrapper_call_with_args_as_a_json_string_still_counts():
    rows = [
        dict(
            status=200,
            request=dict(messages=[dict(role="tool", tool_call_id="c1", content="body")]),
            calls=[
                dict(
                    id="c1",
                    name="tool_call",
                    arguments=json.dumps({"id": "skills_read", "args": json.dumps({"name": "x"})}),
                )
            ],
            usage=None,
        )
    ]
    assert harness.grade(rows, {"x"})["loaded"] == ["x"]


def test_a_gateway_that_fails_to_start_is_stopped(tmp_path, monkeypatch):
    stopped = []

    class Exited:
        def __init__(self, *args, **kwargs):
            pass

        def poll(self):
            return 1

    monkeypatch.setattr(harness.subprocess, "Popen", Exited)
    monkeypatch.setattr(harness, "stop_gateway", stopped.append)
    toml = tmp_path / "g.toml"
    toml.write_text("", "utf-8")
    with pytest.raises(RuntimeError):
        harness.start_gateway(toml, {})
    assert len(stopped) == 1


def test_jsonl_files_are_read_and_closed(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}\n', "utf-8")
    assert harness.read_jsonl(path) == [{"a": 1}, {"a": 2}]
    path.unlink()  # Nothing holds it, so this works on Windows too.
    assert harness.read_jsonl(path) == []


def test_the_gateway_file_keeps_windows_paths_exact():
    """Review of PR 285: a backslash in a path must not become a TOML escape."""
    import tomllib

    folder = r"C:\Users\new\tmp\out"
    manifest, artifact = r"C:\models\unit\m.json", r"C:\models\unit\a.gguf"
    text = harness.gateway_toml(
        harness.ARMS["local"], folder, port=1, local=(manifest, artifact, "http://127.0.0.1:2")
    )
    parsed = tomllib.loads(text)
    assert parsed["store"]["path"] == folder + "/gateway.sqlite3"
    assert parsed["sandbox"]["work_dir"] == folder + "/sandbox"
    assert parsed["local_model"]["manifest"] == manifest
    assert parsed["local_model"]["artifact"] == artifact


def test_a_missing_model_key_is_found_before_any_spending(monkeypatch):
    """Review of PR 285: a clear message, not a KeyError after setup."""
    monkeypatch.delenv("RELAYMIND_EXTERNAL_API_KEY", raising=False)
    args = argparse.Namespace(
        tools=False,
        catalog_dir=None,
        cases=None,
        similar="development",
        arms="default",
        third_party_url=None,
        local_manifest=None,
        local_artifact=None,
    )
    assert "RELAYMIND_EXTERNAL_API_KEY" in harness.setup_problem(args)


def test_the_host_gets_its_own_home_on_every_platform(tmp_path, monkeypatch):
    """Review of PR 285: Node reads USERPROFILE on Windows, so both point at the sandbox."""
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "secret")
    env = harness.host_env(tmp_path / "home", tmp_path / "state", "token")
    assert env["HOME"] == env["USERPROFILE"] == str(tmp_path / "home")
    assert env["OPENCLAW_STATE_DIR"] == str(tmp_path / "state")
    assert env["RELAYMIND_APP_TOKEN"] == "token"
    assert "RELAYMIND_EXTERNAL_API_KEY" not in env


def test_the_windows_host_command_is_the_cmd_shim():
    """Review of PR 285: npm writes openclaw.cmd for Windows."""
    assert harness.openclaw_command("win32").name == "openclaw.cmd"
    assert harness.openclaw_command("darwin").name == "openclaw"


def _upstream(status, body):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post(port, run):
    import httpx

    return httpx.post(
        f"http://127.0.0.1:{port}/run/{run}/v1/chat/completions",
        json=dict(model="m", messages=[], stream=True),
        timeout=10,
        trust_env=False,
    )


def test_the_recorder_passes_an_upstream_error_status_through(tmp_path):
    """Review of PR 285: the host sees the real status, and the row records it."""
    upstream = _upstream(429, b'{"error":"slow down"}')
    try:
        base = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        recorder = harness.Recorder(base, "key", tmp_path)
        answer = _post(recorder.port, "r1")
        assert answer.status_code == 429
        assert answer.json() == {"error": "slow down"}
        # The row is written after the answer is sent, so wait for it.
        assert recorder.wait_idle("r1")
        assert harness.read_jsonl(tmp_path / "r1.jsonl")[0]["status"] == 429
        recorder.close()
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_the_recorder_answers_502_when_the_upstream_cannot_be_reached(tmp_path):
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    recorder = harness.Recorder(f"http://127.0.0.1:{closed}/v1", "key", tmp_path)
    try:
        assert _post(recorder.port, "r2").status_code == 502
        assert recorder.wait_idle("r2")
        assert harness.read_jsonl(tmp_path / "r2.jsonl")[0]["status"] is None
    finally:
        recorder.close()


# --- self-review before the next round of PR 285 ----------------------------------------------


def _exchange(calls, results=(), assistant_ids=None):
    """One recorded exchange: the request so far, then the calls the model made in its answer."""
    messages = []
    if assistant_ids is not None:
        messages.append(dict(role="assistant", tool_calls=[dict(id=i) for i in assistant_ids]))
    messages += [dict(role="tool", tool_call_id=i, content=text) for i, text in results]
    return dict(status=200, request=dict(messages=messages), calls=calls, usage=None)


@pytest.mark.parametrize(
    "arguments",
    [
        {"id": "skills_read", "name": "x"},
        {"id": "skills_read", "args": {}, "name": "x"},
        {"id": "skills_read", "args": {"name": "x"}, "args.other": 1},
    ],
)
def test_wrapper_arguments_are_read_the_way_the_host_reads_them(arguments):
    rows = [
        _exchange([dict(id="c1", name="tool_call", arguments=json.dumps(arguments))]),
        _exchange([], results=[("c1", "body")], assistant_ids=["c1"]),
    ]
    assert harness.grade(rows, {"x"})["loaded"] == ["x"]


def test_a_long_call_id_cut_by_the_host_still_finds_its_result():
    raw = "chatcmpl-tool-" + "a" * 32
    rows = [
        _exchange([dict(id=raw, name="skills_read", arguments='{"name":"x"}')]),
        _exchange(
            [],
            results=[(raw.replace("-", "")[:40], "body")],
            assistant_ids=[raw.replace("-", "")[:40]],
        ),
    ]
    graded = harness.grade(rows, {"x"})
    assert graded["loaded"] == ["x"] and graded["unmatched"] == 0


def test_a_reused_call_id_is_paired_with_its_own_result():
    """The host renames the second call_0, so each call is paired by position, not by name."""
    refused = '{"status":"error","error":"not selected"}'
    rows = [
        _exchange([dict(id="call_0", name="skills_read", arguments='{"name":"x"}')]),
        _exchange(
            [dict(id="call_0", name="skills_read", arguments='{"name":"x"}')],
            results=[("call0", refused)],
            assistant_ids=["call0"],
        ),
        _exchange([], results=[("call0", refused), ("call02", "body")], assistant_ids=["call02"]),
    ]
    graded = harness.grade(rows, {"x"})
    assert graded["refused"] == 1 and graded["loaded"] == ["x"]


def test_unknown_token_usage_is_not_reported_as_zero():
    graded = harness.grade([_exchange([])], {"x"})
    assert graded["total_input"] is None and graded["first_input"] is None


def test_a_run_whose_stream_broke_counts_as_upstream_failed():
    row = dict(_exchange([]), complete=False)
    assert harness.grade([row], {"x"})["upstream_failed"] is True


def test_old_skills_are_cleared_before_installing(tmp_path):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "catalog.json").write_text('{"assets": [], "evidence": []}', "utf-8")
    stale = tmp_path / "skills" / "old-skill"
    stale.mkdir(parents=True)
    harness.install_skills(catalog, tmp_path / "skills")
    assert not stale.exists()


def test_a_host_that_times_out_is_stopped_with_its_children(tmp_path):
    import sys
    import time

    child = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
    )
    started = time.monotonic()
    assert harness.run_host([sys.executable, "-c", child], dict(os.environ), timeout=1) == "timeout"
    assert time.monotonic() - started < 20


def _sse_upstream(lines):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for line in lines:
                self.wfile.write((line + "\n\n").encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_the_recorder_assembles_a_streamed_answer(tmp_path):
    chunk = {
        "choices": [
            {
                "delta": {
                    "content": "hi",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "function": {"name": "skills_read", "arguments": "{}"},
                        }
                    ],
                }
            }
        ]
    }
    usage = {
        "choices": [{"delta": None, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }
    upstream = _sse_upstream(
        [f"data: {json.dumps(chunk)}", f"data:{json.dumps(usage)}", "data: [DONE]"]
    )
    try:
        recorder = harness.Recorder(
            f"http://127.0.0.1:{upstream.server_address[1]}/v1", "k", tmp_path
        )
        assert _post(recorder.port, "ok").status_code == 200
        assert recorder.wait_idle("ok")
        row = harness.read_jsonl(tmp_path / "ok.jsonl")[0]
        assert row["complete"] is True and row["text"] == "hi"
        assert row["calls"][0]["name"] == "skills_read" and row["usage"]["prompt_tokens"] == 5
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_the_recorder_marks_a_broken_stream(tmp_path):
    upstream = _sse_upstream(["data: {not json", 'data: {"error": {"message": "overloaded"}}'])
    try:
        recorder = harness.Recorder(
            f"http://127.0.0.1:{upstream.server_address[1]}/v1", "k", tmp_path
        )
        _post(recorder.port, "bad")
        assert recorder.wait_idle("bad")
        assert harness.read_jsonl(tmp_path / "bad.jsonl")[0]["complete"] is False
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_similar_skills_of_an_earlier_split_are_cleared(tmp_path):
    stale = tmp_path / "skills" / "old-skill"
    stale.mkdir(parents=True)
    harness.install_similar("development", tmp_path / "skills")
    assert not stale.exists()


_HOST_ARGS = json.loads(
    (
        Path(__file__).resolve().parent.parent
        / "integrations/openclaw/test/fixtures/guidance-tool-call-args.json"
    ).read_text("utf-8")
)["cases"]


@pytest.mark.parametrize("case", _HOST_ARGS, ids=lambda c: json.dumps(c["params"]))
def test_the_grader_reads_tool_call_arguments_as_the_host_does(case):
    """The same table the plugin's tests check against the pinned host's own code."""
    tools = set(case.get("tools", []))
    assert harness._host_call(case["params"], tools) == (case["target"], case["args"])


def test_a_third_party_arm_does_not_fall_back_unseen():
    """A selector that is down must give no hint, so the preflight stops the arm."""
    import tomllib

    text = harness.gateway_toml(
        harness.ARMS["third_party"],
        "/tmp/x",
        port=1,
        local=None,
        third_party_url="http://127.0.0.1:9/select",
    )
    assert tomllib.loads(text)["guidance"]["fallback"] is False


def _offered(*names):
    return [dict(type="function", function=dict(name=n)) for n in names]


@pytest.mark.parametrize("written", ["Skills_Read", " skills_read ", "functions.skills_read"])
def test_the_grader_repairs_a_tool_name_as_the_host_does(written):
    rows = [
        dict(
            _exchange([dict(id="c1", name=written, arguments='{"name":" x "}')]),
            request=dict(messages=[], tools=_offered("skills_read", "read")),
        ),
        _exchange([], results=[("c1", "body")], assistant_ids=["c1"]),
    ]
    assert harness.grade(rows, {"x"})["loaded"] == ["x"]


def test_a_read_path_with_a_broken_xml_end_still_counts():
    assert harness._skill_file_folder("/s/pdf-merge/SKILL.md</arg_value>>") == "pdf-merge"


def test_similar_skills_cannot_be_mixed_with_a_catalog():
    args = argparse.Namespace(
        tools=False,
        catalog_dir="c",
        cases="d",
        similar="development",
        arms="default",
        third_party_url=None,
        local_manifest=None,
        local_artifact=None,
        model="m",
        lanes=None,
    )
    os.environ.setdefault("RELAYMIND_EXTERNAL_API_KEY", "unit")
    assert "--similar" in harness.setup_problem(args)


def test_similar_skills_get_their_own_lanes_by_default():
    assert harness.default_lanes(similar=True) == "similar_en,similar_zh"
    assert "en_to_en" in harness.default_lanes(similar=False)


def test_a_skill_name_after_a_byte_order_mark_or_with_a_comment_is_read(tmp_path):
    catalog = tmp_path / "catalog"
    (catalog / "e").mkdir(parents=True)
    (catalog / "e" / "a.md").write_text(
        "\ufeff---\nname: pdf-merge # the merger\n---\nBody", "utf-8"
    )
    (catalog / "catalog.json").write_text(
        json.dumps(
            dict(
                assets=[dict(record=dict(kind="skill", asset_id="s:a"), original_evidence_id="e1")],
                evidence=[dict(id="e1", path="e/a.md")],
            )
        ),
        "utf-8",
    )
    assert harness.install_skills(catalog, tmp_path / "skills") == {"s:a": "pdf-merge"}


def test_a_repeated_skill_name_keeps_the_first_skill(tmp_path):
    catalog = tmp_path / "catalog"
    (catalog / "e").mkdir(parents=True)
    for n in (1, 2):
        (catalog / "e" / f"{n}.md").write_text(f"---\nname: same\n---\nBody {n}", "utf-8")
    (catalog / "catalog.json").write_text(
        json.dumps(
            dict(
                assets=[
                    dict(record=dict(kind="skill", asset_id=f"s:{n}"), original_evidence_id=f"e{n}")
                    for n in (1, 2)
                ],
                evidence=[dict(id=f"e{n}", path=f"e/{n}.md") for n in (1, 2)],
            )
        ),
        "utf-8",
    )
    assert harness.install_skills(catalog, tmp_path / "skills") == {"s:1": "same"}
    assert "Body 1" in (tmp_path / "skills" / "same" / "SKILL.md").read_text("utf-8")


@pytest.mark.parametrize("bad", ["../outside", "ABSOLUTE", "a/b", "..", "C:\\x"])
def test_a_skill_name_that_is_not_one_folder_is_not_installed(tmp_path, bad):
    # An absolute name points inside this test's own folder, never at a real system path.
    bad = str(tmp_path / "outside") if bad == "ABSOLUTE" else bad
    catalog = tmp_path / "catalog"
    (catalog / "e").mkdir(parents=True)
    (catalog / "e" / "a.md").write_text(f"---\nname: {bad}\n---\nBody", "utf-8")
    (catalog / "catalog.json").write_text(
        json.dumps(
            dict(
                assets=[dict(record=dict(kind="skill", asset_id="s:a"), original_evidence_id="e1")],
                evidence=[dict(id="e1", path="e/a.md")],
            )
        ),
        "utf-8",
    )
    assert harness.install_skills(catalog, tmp_path / "skills") == {}
    assert not (tmp_path / "outside").exists()


def test_a_run_name_is_one_safe_folder():
    assert harness.run_name("default", "../x/y") == "default__x_y"


def test_the_preflight_includes_a_chinese_similar_prompt():
    sample = [dict(id="a", lane="similar_en"), dict(id="b", lane="similar_zh")]
    assert [c["id"] for c in harness.preflight_cases(sample)] == ["a", "b"]


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_a_gateway_is_stopped_with_its_children(monkeypatch, platform):
    """Review of PR 285: the whole tree goes, and on Windows before the parent is gone."""
    import signal
    import subprocess

    signals, stopped = [], []

    class Stubborn:
        pid = 4242

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("gateway", timeout)

    monkeypatch.setattr(harness.sys, "platform", platform)
    monkeypatch.setattr(
        harness.os, "killpg", lambda pid, sig: signals.append((pid, sig)), raising=False
    )
    monkeypatch.setattr(harness, "_stop_tree", stopped.append)
    process = Stubborn()
    harness.stop_gateway(process)
    assert stopped == [process]
    assert signals == ([] if platform == "win32" else [(4242, signal.SIGTERM)])


def test_a_failed_run_leaves_no_capture_or_state(tmp_path):
    state, capture = tmp_path / "state", tmp_path / "cap.jsonl"
    state.mkdir()
    capture.write_text("{}", "utf-8")
    with pytest.raises(RuntimeError), harness.disposable(state, capture, keep=False):
        raise RuntimeError("grading failed")
    assert not state.exists() and not capture.exists()
