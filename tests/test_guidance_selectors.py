"""The third-party example and the benchmark wrapper (RM28, RM35, RM45; AC26, AC38)."""

import ast
import asyncio
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from helpers import load_script_module
from relaymind.guidance import GuidanceSettings, parse_guidance_request, run_guidance
from relaymind.selection.asset_admission import validate_against_offer
from relaymind.selection.asset_contract import (
    SelectorIdentity,
    parse_asset_request,
    parse_asset_result,
)
from relaymind.selection.asset_http import HttpAssetSelector
from test_asset_selection import record, request

ROOT = Path(__file__).resolve().parent.parent
example = load_script_module("example_intent_selector")
wrapper = load_script_module("guidance_selector_server")


def catalog():
    def skill(name, description):
        value = record(asset_id=f"s:{name}", description=description)
        value["identity"] = dict(package_id=name, package_version=None)
        return value

    return [
        skill("pdf-merge", "Merge several pdf documents into one file"),
        skill("email-writer", "Write and send an email message"),
    ]


def v2(text="merge these pdf files", **changes):
    return request(task_text=text, catalog=catalog(), requested_kinds=["skill"], **changes)


def admitted(raw, value, name, version="1"):
    parsed_request = parse_asset_request(value)
    parsed = parse_asset_result(raw)
    validate_against_offer(parsed, parsed_request, SelectorIdentity(name, version), http=True)
    return [c["asset_id"] for c in parsed.data["candidates"]]


def test_the_example_needs_nothing_from_relaymind():
    tree = ast.parse((ROOT / "scripts/example_intent_selector.py").read_text("utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(__import__("sys").stdlib_module_names)


def test_the_example_answers_a_valid_ranked_result_with_no_model():
    value = v2()
    assert admitted(example.select(value), value, "example_intent")[0] == "s:pdf-merge"


def test_the_example_answers_no_match_when_nothing_fits():
    value = v2("zzzz qqqq")
    raw = example.select(value)
    assert raw["outcome"] == "no_match"
    assert raw["decision"] == "no_match"
    assert admitted(raw, value, "example_intent") == []


def test_the_example_writes_keywords_only_for_a_non_latin_prompt():
    calls = []

    def chat(system, user, max_tokens):
        calls.append(system)
        return "merge pdf documents"

    value = v2("把这些文件合并")
    assert admitted(example.select(value, chat=chat), value, "example_intent")[0] == "s:pdf-merge"
    assert len(calls) == 2  # keywords, then the picker fails to parse and ranking stands
    calls.clear()
    example.select(v2(), chat=chat, pick=False)
    assert calls == []


def test_the_example_lets_the_model_pick_from_the_shortlist():
    def chat(system, user, max_tokens):
        return '{"picks":[2]}' if "numbered list" in system else "words"

    many = v2("pdf email message merge")
    assert admitted(example.select(many, chat=chat), many, "example_intent")[0] == "s:email-writer"


def test_a_failing_model_leaves_the_word_match_answer():
    def chat(system, user, max_tokens):
        raise RuntimeError("down")

    value = v2()
    assert admitted(example.select(value, chat=chat), value, "example_intent")[0] == "s:pdf-merge"


def serving(handler_class):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/select"


def test_the_gateway_uses_the_example_over_real_http():
    server, url = serving(example.handler())
    try:
        settings = GuidanceSettings(
            enabled=True, primary="http", name="example_intent", version="1", url=url
        )
        selector = HttpAssetSelector(name="example_intent", version="1", url=url)
        parsed = parse_guidance_request(
            dict(
                schema_version=1,
                task_text="send an email message",
                assets=[
                    dict(name="pdf-merge", kind="skill", description="Merge pdf documents"),
                    dict(name="email-writer", kind="skill", description="Write and send an email"),
                ],
            )
        )
        result = asyncio.run(run_guidance(parsed, settings, selector=selector))
    finally:
        server.shutdown()
    assert result["chosen_by"] == "third_party"
    assert result["degraded"] is False
    assert result["picks"][0]["name"] == "email-writer"


def test_the_wrapper_answers_v2_from_the_guidance_stages():
    value = v2()
    settings = GuidanceSettings(enabled=True, unpicked_limit=8)
    raw = asyncio.run(wrapper.select(value, settings, name="guidance_word", version="1"))
    assert admitted(raw, value, "guidance_word")[0] == "s:pdf-merge"
    assert raw["mechanism"] == "guidance_word_match"


def test_the_wrapper_reports_no_match_and_failures_as_a_valid_result():
    value = v2("zzzz qqqq")
    settings = GuidanceSettings(enabled=True)
    raw = asyncio.run(wrapper.select(value, settings, name="guidance_word", version="1"))
    assert raw["outcome"] == "no_match"
    assert admitted(raw, value, "guidance_word") == []


def test_the_wrapper_keeps_tool_names_for_word_match():
    tool = record(asset_id="t:add", kind="mcp_tool", description="Adds a thing")
    tool["identity"] = dict(
        server_id="github",
        tool_name="add_comment",
        exposed_name="github__add_comment",
        input_schema={"type": "object"},
        schema_hash=__import__("relaymind.canonical", fromlist=["x"]).canonical_digest(
            {"type": "object"}
        ),
    )
    value = request(
        task_text="add_comment please",
        catalog=[tool, *catalog()],
        requested_kinds=["skill", "mcp_tool"],
    )
    settings = GuidanceSettings(enabled=True)
    raw = asyncio.run(wrapper.select(value, settings, name="g", version="1"))
    assert raw["candidates"][0]["asset_id"] == "t:add"


def test_result_json_is_plain():
    assert json.dumps(example.select(v2()))


@pytest.mark.parametrize("bad", ["x", {"schema_version": 1}])
def test_the_example_refuses_a_bad_request(bad):
    with pytest.raises((ValueError, KeyError, TypeError)):
        example.select(bad)


def test_the_wrapper_makes_a_failed_stage_visible_in_the_evidence():
    async def down(messages, max_tokens):
        raise RuntimeError("connection refused")

    value = v2("把 pdf 文件 merge")
    settings = GuidanceSettings(enabled=True, keywords="local")
    raw = asyncio.run(wrapper.select(value, settings, name="g", version="1", keywords=down))
    assert raw["mechanism"].endswith("_degraded")


def test_the_example_treats_vietnamese_as_latin():
    assert example.needs_keywords("Tóm tắt tài liệu") is False
    assert example.needs_keywords("合并文件") is True


def test_the_example_leaves_a_thinking_model_room_to_answer():
    # A model that thinks spends its output limit before it answers: a small limit returns nothing.
    limits = []

    def chat(system, user, max_tokens):
        limits.append(max_tokens)
        return '{"picks":[1]}' if "numbered list" in system else "merge pdf"

    example.select(v2("把这些文件合并"), chat=chat)
    assert len(limits) == 2
    assert min(limits) >= 300


def test_the_example_sends_the_reasoning_effort_it_was_given(monkeypatch):
    sent = {}

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(dict(choices=[dict(message=dict(content="ok"))])).encode()

    def urlopen(request, timeout):
        sent.update(json.loads(request.data))
        return Answer()

    monkeypatch.setattr(example.urllib.request, "urlopen", urlopen)
    assert example.model_client("http://x/v1", None, None, "low")("s", "u", 9) == "ok"
    assert sent["reasoning_effort"] == "low"
    example.model_client("http://x/v1", None, None)("s", "u", 9)


def test_the_benchmark_server_clips_names_and_descriptions_by_bytes_not_letters():
    """Review of PR 285: a long Chinese description must not make the request invalid."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "gss", root / "scripts/guidance_selector_server.py"
    )
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    assert len(server.clip_bytes("合" * 2000, 2048).encode("utf-8")) <= 2048
    assert server.clip_bytes("合并" * 100, 128).encode("utf-8").decode("utf-8")
    assert server.clip_bytes("short", 128) == "short"


def test_the_example_reads_its_answer_when_braces_follow_it():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "eis", root / "scripts/example_intent_selector.py"
    )
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    text = '```json\n{"picks":[2,1]}\n```\nI chose {item 2} first.'
    assert example.first_json_object(text, "picks") == {"picks": [2, 1]}
    assert example.first_json_object('{not json} then {"picks":[1]}', "picks") == {"picks": [1]}
    assert example.first_json_object("nothing here", "picks") is None


def test_the_example_scores_its_candidates_in_the_order_it_ranks_them():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "eis2", root / "scripts/example_intent_selector.py"
    )
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)

    def record(name, description):
        return dict(
            asset_id=f"a:{name}",
            kind="skill",
            identity=dict(package_id=name),
            description=description,
            content_hash="h",
            source_version=None,
        )

    catalog = [record("weak", "pdf"), record("strong", "pdf merge files merge pdf documents")]
    value = dict(
        schema_version=2,
        recommendation_id="r",
        catalog_revision=1,
        host_snapshot_revision=None,
        task_text="merge pdf files",
        catalog=catalog,
        candidate_limit=5,
    )
    result = example.select(value, chat=lambda system, user, tokens: '{"picks":[2,1]}')
    scores = [c["score"] for c in result["candidates"]]
    assert [c["asset_id"] for c in result["candidates"]] == ["a:weak", "a:strong"]
    assert scores == sorted(scores, reverse=True)


def test_the_example_keeps_the_word_match_order_when_the_answer_names_no_number():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "eis3", root / "scripts/example_intent_selector.py"
    )
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    record = dict(
        asset_id="a:pdf",
        kind="skill",
        identity=dict(package_id="pdf"),
        description="merge pdf files",
        content_hash="h",
        source_version=None,
    )
    value = dict(
        schema_version=2,
        recommendation_id="r",
        catalog_revision=1,
        host_snapshot_revision=None,
        task_text="merge pdf files",
        catalog=[record],
        candidate_limit=5,
    )
    result = example.select(value, chat=lambda system, user, tokens: '{"picks":["pdf"]}')
    assert [c["asset_id"] for c in result["candidates"]] == ["a:pdf"]
    nothing = example.select(value, chat=lambda system, user, tokens: '{"picks":[]}')
    assert nothing["candidates"] == []


def test_the_example_removes_an_unclosed_thinking_block():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "eis4", root / "scripts/example_intent_selector.py"
    )
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    assert example.strip_thinking("<think>never closed") == ""
    assert example.strip_thinking('<think>a</think>{"picks":[1]}') == '{"picks":[1]}'


def test_the_wrapper_passes_earlier_user_text_as_recent_text():
    """Review of PR 285: a follow-up through the wrapper needs the conversation."""
    value = v2("yes, do that", conversation=[dict(role="user", text="merge these pdf files")])
    settings = GuidanceSettings(enabled=True)
    raw = asyncio.run(wrapper.select(value, settings, name="g", version="1"))
    assert admitted(raw, value, "g")[:1] == ["s:pdf-merge"]


def test_the_wrapper_answers_only_on_select():
    """Review of PR 285: like the example, any other path is 404."""
    import urllib.error
    import urllib.request

    server = ThreadingHTTPServer(("127.0.0.1", 0), wrapper.make_handler(lambda value: {"ok": 1}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{base}/select", b"{}")) as answer:
            assert json.load(answer) == {"ok": 1}
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(urllib.request.Request(f"{base}/other", b"{}"))
        assert refused.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_the_example_reads_earlier_user_text_for_a_short_follow_up():
    """Review of PR 285: the gateway sends recent_text as the conversation."""
    earlier = [dict(role="user", text="merge these pdf files")]
    raw = example.select(v2("yes, do that", conversation=earlier))
    assert [c["asset_id"] for c in raw["candidates"]][:1] == ["s:pdf-merge"]
    # A new request of its own is read alone.
    raw = example.select(v2("write and send an email message to the team", conversation=earlier))
    assert [c["asset_id"] for c in raw["candidates"]] == ["s:email-writer"]


def test_the_wrapper_names_a_missing_model_setting(monkeypatch, capsys):
    """Review of PR 285: a clear message, not a KeyError."""
    for name in ("RELAYMIND_EXTERNAL_BASE_URL", "RELAYMIND_EXTERNAL_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sys.argv", ["gss", "--port", "1", "--name", "g", "--picker", "external"])
    with pytest.raises(SystemExit):
        wrapper.main()
    assert "RELAYMIND_EXTERNAL_BASE_URL" in capsys.readouterr().err


def test_the_wrapper_needs_the_model_key_for_an_external_stage(monkeypatch, capsys):
    monkeypatch.setenv("RELAYMIND_EXTERNAL_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("RELAYMIND_EXTERNAL_MODEL", "m")
    monkeypatch.delenv("RELAYMIND_EXTERNAL_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["gss", "--port", "1", "--name", "g", "--picker", "external"])
    with pytest.raises(SystemExit):
        wrapper.main()
    assert "RELAYMIND_EXTERNAL_API_KEY" in capsys.readouterr().err


def test_the_wrapper_offers_records_whose_names_collide():
    first, second = catalog()[0], catalog()[0]
    second = dict(second, asset_id="s:pdf-merge-2", content_hash="b" * 64)
    value = request(
        task_text="merge these pdf files",
        catalog=[first, second],
        requested_kinds=["skill"],
    )
    settings = GuidanceSettings(enabled=True)
    raw = asyncio.run(wrapper.select(value, settings, name="g", version="1"))
    assert {c["asset_id"] for c in raw["candidates"]} == {"s:pdf-merge", "s:pdf-merge-2"}


@pytest.mark.parametrize(
    ("text", "word"),
    [("Объединить PDF файлы", "объединить"), ("résumé café", "résumé"), ("合并文件", "合并")],
)
def test_the_example_reads_words_of_every_script(text, word):
    assert word in example.words(text)


def test_the_example_says_when_its_model_failed():
    """Review of PR 285: a report must not claim a model arm that fell back to word match."""

    def down(system, user, max_tokens):
        raise KeyError("MY_KEY")

    raw = example.select(v2("把 pdf 文件 merge"), chat=down)
    assert raw["mechanism"].endswith("_degraded")
    assert example.select(v2(), chat=None)["mechanism"] == "example_intent_rank"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--model-url", "http://127.0.0.1:9/v1"], "--model"),
        (
            [
                "--model-url",
                "http://127.0.0.1:9/v1",
                "--model",
                "m",
                "--api-key-env",
                "NO_SUCH_KEY_X",
            ],
            "NO_SUCH_KEY_X",
        ),
    ],
)
def test_the_example_refuses_a_model_setup_it_cannot_use(argv, message, monkeypatch, capsys):
    monkeypatch.delenv("NO_SUCH_KEY_X", raising=False)
    monkeypatch.setattr("sys.argv", ["eis", *argv])

    def no_server(*a, **k):
        raise AssertionError("main() should have refused this setup before serving")

    monkeypatch.setattr(example, "ThreadingHTTPServer", no_server)
    with pytest.raises(SystemExit):
        example.main()
    assert message in capsys.readouterr().err


def test_the_wrapper_keeps_a_long_colliding_name_within_the_limit():
    long_id = "s:" + "v" * 200
    first, second = catalog()[0], catalog()[0]
    second = dict(second, asset_id=long_id, content_hash="c" * 64)
    value = request(
        task_text="merge these pdf files", catalog=[first, second], requested_kinds=["skill"]
    )
    raw = asyncio.run(wrapper.select(value, GuidanceSettings(enabled=True), name="g", version="1"))
    assert {c["asset_id"] for c in raw["candidates"]} == {"s:pdf-merge", long_id}


def test_the_example_writes_keywords_from_the_earlier_text_for_a_follow_up():
    sent = []

    def chat(system, user, max_tokens):
        sent.append(user)
        return "merge pdf"

    earlier = [dict(role="user", text="把这些 pdf 文件合并")]
    example.select(v2("是的，就这么做", conversation=earlier), chat=chat, pick=False)
    assert "合并" in sent[0]


def test_the_wrapper_reuses_the_gateways_byte_clipping():
    from relaymind.guidance import pipeline

    assert wrapper.clip_bytes is pipeline.clip_bytes
