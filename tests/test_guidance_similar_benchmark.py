"""The data and harness for similar assets stay sound (RM16, RM45; spec 0.69 section 7.7)."""

import asyncio
import importlib.util
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gsb", ROOT / "scripts/guidance_similar_benchmark.py")
harness = importlib.util.module_from_spec(spec)
sys.modules["gsb"] = harness
spec.loader.exec_module(harness)


@pytest.mark.parametrize(("split", "skills"), [("development", 32), ("holdout", 70)])
def test_each_split_has_every_kind_of_prompt(split, skills):
    assets, cases = harness.load_cases(split)
    assert len(assets) == skills
    assert len({a["name"] for a in assets}) == skills
    lanes = {c["lane"] for c in cases}
    assert {"en", "zh", "follow_up_en", "follow_up_zh", "no_signal_en", "no_signal_zh"} <= lanes
    assert {"unrelated_en", "unrelated_zh"} <= lanes
    assert len({c["case"] for c in cases}) == len(cases)


def test_the_splits_share_no_skill():
    development = {a["name"] for a in harness.load_cases("development")[0]}
    holdout = {a["name"] for a in harness.load_cases("holdout")[0]}
    assert not development & holdout


def test_a_follow_up_carries_the_earlier_request_and_names_no_skill():
    _, cases = harness.load_cases("development")
    follow_ups = [c for c in cases if c["lane"] == "follow_up_en"]
    assert all(c["recent"] and c["expected"][0] not in c["text"] for c in follow_ups)


@pytest.mark.parametrize(
    ("arm", "message"),
    [
        ("third_party", "--selector-url"),
        ("external_picker", "--model-base-url"),
        ("local", "--local-url"),
    ],
)
def test_an_arm_without_its_model_is_refused_so_its_label_cannot_mislead(arm, message):
    assert message in harness.arm_problem(arm, None, None, None)


def test_a_complete_arm_can_run(monkeypatch):
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "unit")
    assert harness.arm_problem("word", None, None, None) is None
    assert harness.arm_problem("local_picker", "m", "l", None) is None


def test_the_grade_counts_a_right_first_pick_and_a_sibling_first_pick():
    cases = [
        dict(lane="en", expected=["a"], family=["a", "b"]),
        dict(lane="en", expected=["a"], family=["a", "b"]),
    ]
    rows = [
        dict(picks=["a"], degraded=False, decision="picks"),
        dict(picks=["b", "a"], degraded=False, decision="picks"),
    ]
    entry = harness.grade(cases, rows)["en"]
    assert (entry["first_pick"], entry["first_pick_sibling"], entry["recall"]) == (1, 1, 2)


@pytest.mark.parametrize("split", ["development", "holdout"])
def test_each_split_has_complex_requests_that_need_several_skills(split):
    assets, cases = harness.load_cases(split)
    names = {a["name"] for a in assets}
    complex_cases = [c for c in cases if c["lane"].startswith("complex")]
    assert complex_cases
    for case in complex_cases:
        assert 3 <= len(case["expected"]) <= 5
        assert set(case["expected"]) <= names


def test_the_grade_reports_how_much_of_a_set_was_found():
    cases = [dict(lane="complex_en", expected=["a", "b"], family=[])]
    rows = [dict(picks=["a", "q"], degraded=False, decision="picks")]
    entry = harness.grade(cases, rows)["complex_en"]
    assert entry["found_share"] == 0.5
    assert entry["all_found"] == 0


def test_every_arm_can_be_set_through_the_gateways_own_settings():
    """Review of PR 285: a report must be reproducible through the gateway, and say who chose."""
    from relaymind.guidance import parse_guidance_settings

    selector = dict(name="team", version="1", url="http://127.0.0.1:9/select")
    for stages in harness.ARMS.values():
        settings = dict(stages, **selector) if stages.get("primary") == "http" else stages
        assert parse_guidance_settings(settings).primary == stages.get("primary", "builtin")


def test_an_external_arm_without_its_key_is_refused(monkeypatch):
    """Review of PR 285: a missing key would degrade every case to word match."""
    monkeypatch.delenv("UNIT_KEY", raising=False)
    message = harness.arm_problem("external_picker", "m", None, None, api_key_env="UNIT_KEY")
    assert "UNIT_KEY" in message
    monkeypatch.setenv("UNIT_KEY", "k")
    assert harness.arm_problem("external_picker", "m", None, None, api_key_env="UNIT_KEY") is None


def test_an_external_arm_without_a_model_name_is_refused(monkeypatch):
    """Review of PR 285: without a model the provider refuses every call."""
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "k")
    assert "--model" in harness.arm_problem("external_picker", "m", None, None, model=None)
    assert harness.arm_problem("external_picker", "m", None, None, model="x") is None


def test_request_options_are_json_values_and_follow_the_gateway_rules():
    assert harness.parse_options(["reasoning_effort=low", 'thinking={"type":"disabled"}']) == {
        "reasoning_effort": "low",
        "thinking": {"type": "disabled"},
    }
    with pytest.raises(ValueError):
        harness.parse_options(["model=other"])


# --- a third-party selector that indexes, synced before measuring (spec 0.73 7.7; RM47; AC42) ---


@pytest.fixture
def indexing_selector(tmp_path):
    """The reference selector with a sync index: not_ready until each offered pair is synced."""
    from helpers import load_script_module
    from relaymind.guidance import GuidanceSettings

    server = load_script_module("guidance_selector_server")
    index = server.SyncIndex([tmp_path])
    settings = GuidanceSettings(enabled=True)
    state = dict(uploads=[], sync_fails=False)

    def answer(value):
        return asyncio.run(server.select(value, settings, name="router", version="1", index=index))

    def sync(value):
        if state["sync_fails"]:
            raise ValueError("sync is down")
        if value["operation"] == "upload":
            state["uploads"].extend(value["assets"])
        return index.handle(value)

    http = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(answer, sync))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http.server_port}"
    yield SimpleNamespace(select=f"{base}/select", sync=f"{base}/sync", state=state)
    http.shutdown()


def _third_party(selector, *extra):
    return harness.build_parser().parse_args(
        [
            *("--split", "development", "--arm", "third_party", "--output", "unused.json"),
            *("--selector-url", selector.select, "--selector-name", "router", "--limit", "6"),
            *extra,
        ]
    )


def test_without_sync_an_indexing_selector_answers_no_case(indexing_selector):
    """The gap of PR 291's review: a cold selector says not_ready, so word match answers."""
    report = asyncio.run(harness.run(_third_party(indexing_selector)))
    assert report["sync"] is None
    assert all(row["degraded"] for row in report["rows"])
    assert all(row["stages"]["third_party"] == "failed" for row in report["rows"])


def test_with_sync_url_the_selector_is_synced_from_the_json_assets_before_measuring(
    indexing_selector,
):
    """JSON assets have no SKILL.md: each upload is the record alone, holding the whole asset."""
    args = _third_party(indexing_selector, "--sync-url", indexing_selector.sync)
    report = asyncio.run(harness.run(args))
    assert report["sync"]["outcome"] == "ready"
    assert report["sync"]["status"]["last_outcome"] in ("synced", "nothing_missing")
    assert report["sync"]["status"]["files_sent"] == 0
    assert all(not row["degraded"] for row in report["rows"])
    assert all(row["stages"] == {"third_party": "ok"} for row in report["rows"])
    uploads = indexing_selector.state["uploads"]
    assets, _ = harness.load_cases("development")
    assert all(item["content"] is None for item in uploads)
    assert sorted(item["record"]["description"] for item in uploads) == sorted(
        a["description"] for a in assets
    )


def test_a_selector_that_never_gets_ready_is_not_measured(indexing_selector):
    indexing_selector.state["sync_fails"] = True
    args = _third_party(indexing_selector, "--sync-url", indexing_selector.sync)
    args.sync_wait_s = 0.5
    with pytest.raises(SystemExit) as stopped:
        asyncio.run(harness.run(args))
    assert "not ready" in str(stopped.value)


def test_sync_url_needs_the_third_party_arm():
    message = harness.arm_problem("word", None, None, None, sync_url="http://127.0.0.1:9/sync")
    assert "--sync-url" in message
    sync = "http://127.0.0.1:9/sync"
    assert harness.arm_problem("third_party", None, None, "http://x/select", sync_url=sync) is None


def test_sync_url_is_refused_above_one_offer_of_assets():
    """Above 1,000 assets each prompt offers its own subset, so no single sync covers the cases."""
    assert "1000" in harness.offer_problem(1001)
    assert harness.offer_problem(1000) is None


def test_a_no_match_from_the_synced_selector_is_ready(indexing_selector):
    """A no_match from the selector ranked the whole offer: chosen_by is none, yet it is ready."""
    from dataclasses import replace

    from relaymind.guidance import GuidanceSettings
    from relaymind.guidance.sync import GuidanceSync
    from relaymind.selection.asset_http import HttpAssetSelector

    settings = GuidanceSettings(
        enabled=True, primary="http", name="router", version="1", url=indexing_selector.select
    )
    selector = HttpAssetSelector(name="router", version="1", url=indexing_selector.select)
    sync = GuidanceSync(replace(settings, sync_url=indexing_selector.sync))
    assets = [dict(name="pdf-merge", kind="skill", description="Merge pdf documents")]
    synced = asyncio.run(harness.sync_selector(assets, settings, selector, sync, wait_s=10))
    assert synced["outcome"] == "ready" and synced["selects"] == 2


def test_a_background_from_a_source_catalog_matches_what_the_turn_harness_installs(tmp_path):
    """--background-catalog: the skills the turn harness installs and every tool, never a skill
    named like one of the data's own, each with its record's description (spec 0.73 7.7)."""
    import json

    taken = harness.load_cases("holdout")[0][0]["name"]
    catalog = tmp_path / "catalog"
    (catalog / "e").mkdir(parents=True)
    for name in ("pdf-merge", taken):
        (catalog / "e" / f"{name}.md").write_text(f"---\nname: {name}\n---\nBody", "utf-8")
    (catalog / "e" / "zip.md").write_bytes(b"PK\x03\x04")  # The turn harness skips a ZIP.
    skill = lambda asset_id, path: dict(  # noqa: E731
        record=dict(kind="skill", asset_id=asset_id, description=f"About {asset_id} " + "x" * 3000),
        original_evidence_id=path,
    )
    (catalog / "catalog.json").write_text(
        json.dumps(
            dict(
                assets=[
                    skill("s:pdf", "pdf"),
                    skill("s:taken", "taken"),
                    skill("s:zip", "zip"),
                    dict(
                        record=dict(
                            kind="mcp_tool",
                            asset_id="t:issue",
                            description="Open an issue",
                            identity=dict(tool_name="create_issue"),
                        )
                    ),
                ],
                evidence=[
                    dict(id="pdf", path="e/pdf-merge.md"),
                    dict(id="taken", path=f"e/{taken}.md"),
                    dict(id="zip", path="e/zip.md"),
                ],
            )
        ),
        "utf-8",
    )
    background = harness.background_from_catalog(catalog)
    assert [(a["name"], a["kind"]) for a in background] == [
        ("pdf-merge", "skill"),
        ("create_issue", "mcp_tool"),
    ]
    assert background[0]["description"].startswith("About s:pdf")
    assert len(background[0]["description"].encode()) <= 2048
    assert background[1]["description"] == "Open an issue"
