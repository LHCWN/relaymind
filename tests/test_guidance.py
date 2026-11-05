"""Stateless per-prompt guidance over a host-supplied list (RM45, AC38; spec 0.69 section 7.7)."""

import asyncio
import hashlib

import pytest

from relaymind.guidance import (
    GuidanceError,
    GuidanceSettings,
    needs_keywords,
    parse_guidance_request,
    parse_guidance_settings,
    run_guidance,
    validate_guidance_result,
)


def asset(name, description, kind="skill"):
    return dict(name=name, kind=kind, description=description)


CATALOG = [
    asset("pdf-merge", "Merge several pdf documents into one file"),
    asset("email-writer", "Write and send an email message"),
    asset("weather", "Get the current weather forecast for a city"),
    asset("github__add_comment", "Add a comment to a pull request", kind="mcp_tool"),
]


def req(text="please merge these pdf files", assets=None, **extra):
    return parse_guidance_request(
        dict(schema_version=1, task_text=text, assets=assets or CATALOG, **extra)
    )


def run(request, settings=None, **kw):
    return asyncio.run(run_guidance(request, settings or GuidanceSettings(enabled=True), **kw))


def names(result):
    return [p["name"] for p in result["picks"]]


class Chat:
    """A model double: records what it was sent and answers a script."""

    def __init__(self, text="", error=None, delay=0.0, usage=None):
        self.text, self.error, self.delay, self.usage = text, error, delay, usage
        self.calls = []

    async def __call__(self, messages, max_tokens):
        self.calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.text, self.usage


class Script(Chat):
    """A model double that answers each call with the next text."""

    def __init__(self, *texts):
        super().__init__()
        self.texts = list(texts)

    async def __call__(self, messages, max_tokens):
        self.calls.append(messages)
        return self.texts.pop(0), None


# --- settings -------------------------------------------------------------------------------


def test_guidance_is_off_by_default():
    assert parse_guidance_settings({}).enabled is False


@pytest.mark.parametrize(
    "bad",
    [
        {"nope": 1},
        {"enabled": "yes"},
        {"keywords": "cloud"},
        {"picker": "local"},
        {"primary": "other"},
        {"primary": "http"},
        {"primary": "http", "name": "a", "version": "1", "url": "http://example.com/select"},
        {"total_ms": 0},
        {"shortlist": 0},
        {"max_picks": 21},
        {"unpicked_limit": 0},
        {"fallback": "no"},
    ],
)
def test_bad_settings_stop_startup(bad):
    with pytest.raises(ValueError):
        parse_guidance_settings(bad)


def test_http_primary_accepts_a_loopback_url():
    s = parse_guidance_settings(
        {"primary": "http", "name": "a", "version": "1", "url": "http://127.0.0.1:9/select"}
    )
    assert (s.primary, s.name, s.version) == ("http", "a", "1")


# --- request limits -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        dict(schema_version=2, task_text="x", assets=[]),
        dict(schema_version=1, task_text="", assets=[]),
        dict(schema_version=1, task_text="x", assets=[], extra=1),
        dict(schema_version=1, task_text="x", assets=[asset("a", "d", kind="plugin")]),
        dict(schema_version=1, task_text="x", assets=[asset("n" * 129, "d")]),
        dict(schema_version=1, task_text="x", assets=[asset("a", "d" * 2049)]),
        dict(schema_version=1, task_text="x", assets=[asset("a", "d"), asset("a", "e")]),
        dict(schema_version=1, task_text="x", assets=[], max_picks=0),
        dict(schema_version=1, task_text="x", assets=[], max_picks=21),
        dict(schema_version=1, task_text="x" * (512 * 1024 + 1), assets=[]),
    ],
)
def test_invalid_requests_are_refused(bad):
    with pytest.raises(GuidanceError):
        parse_guidance_request(bad)


def test_more_than_the_asset_limit_is_refused():
    assets = [asset(f"a{i}", "d") for i in range(20_001)]
    with pytest.raises(GuidanceError):
        parse_guidance_request(dict(schema_version=1, task_text="x", assets=assets))


def test_the_same_name_in_two_kinds_is_allowed():
    req(assets=[asset("a", "d"), asset("a", "d", kind="mcp_tool")])


# --- word match needs no model --------------------------------------------------------------


def test_word_match_answers_with_no_model_and_puts_the_right_asset_first():
    result = run(req())
    validate_guidance_result(result)
    assert names(result)[0] == "pdf-merge"
    assert result["decision"] == "picks"
    assert result["chosen_by"] == "word_match"
    assert result["degraded"] is False
    assert result["offered"] == 4
    assert [(s["stage"], s["outcome"]) for s in result["stages"]] == [("word_match", "ok")]


def test_no_overlap_is_no_match_not_a_guess():
    result = run(req("zzzz qqqq"))
    assert result["decision"] == "no_match"
    assert result["picks"] == []
    assert result["chosen_by"] == "none"


def test_an_empty_host_list_is_no_match():
    assert run(req(assets=[asset("x", "y")], text="zzzz"))["decision"] == "no_match"
    result = asyncio.run(
        run_guidance(
            parse_guidance_request(dict(schema_version=1, task_text="pdf", assets=[])),
            GuidanceSettings(enabled=True),
        )
    )
    assert result["decision"] == "no_match"
    assert result["offered"] == 0


def test_a_rare_word_outranks_a_common_word():
    common = [asset(f"common-{i}", "file helper") for i in range(30)]
    rare = asset("rare-tool", "seismic helper")
    result = run(req("seismic file", assets=[*common, rare]))
    assert names(result)[0] == "rare-tool"


def test_without_a_picker_the_host_model_chooses_from_a_short_list():
    many = [asset(f"pdf-{i:02}", "pdf tool") for i in range(30)]
    settings = GuidanceSettings(enabled=True, unpicked_limit=8)
    result = run(req("pdf", assets=many), settings)
    assert len(result["picks"]) == 8
    assert result["shortlist"] == 30


def test_a_newly_installed_asset_counts_on_its_first_request():
    assert "new-skill" not in names(run(req("translate a contract")))
    added = [*CATALOG, asset("new-skill", "Translate a legal contract")]
    assert names(run(req("translate a contract", assets=added)))[0] == "new-skill"


def test_tools_are_chosen_like_skills():
    result = run(req("add a comment to the pull request"))
    assert result["picks"][0]["name"] == "github__add_comment"
    assert result["picks"][0]["kind"] == "mcp_tool"


# --- keywords -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("merge two pdf files", False),
        ("fusionner deux fichiers énergie", False),
        ("合并两个 pdf 文件", True),
        ("Объедини два файла", True),
        ("Tóm tắt tài liệu", False),
        ("123 !!!", False),
    ],
)
def test_keywords_are_needed_only_outside_latin_script(text, expected):
    assert needs_keywords(text) is expected


KEYWORDS = GuidanceSettings(enabled=True, keywords="local")


def test_keywords_are_skipped_for_a_latin_prompt():
    chat = Chat("unused")
    result = run(req(), KEYWORDS, keywords=chat)
    assert chat.calls == []
    assert ("keywords", "skipped") in [(s["stage"], s["outcome"]) for s in result["stages"]]


def test_keywords_find_an_english_skill_for_a_chinese_prompt():
    assert run(req("把这些文件合并"))["decision"] == "no_match"
    chat = Chat("merge pdf documents", usage=dict(input_tokens=9, output_tokens=3))
    result = run(req("把这些文件合并"), KEYWORDS, keywords=chat)
    assert names(result)[0] == "pdf-merge"
    stage = next(s for s in result["stages"] if s["stage"] == "keywords")
    assert (stage["outcome"], stage["usage"]) == ("ok", dict(input_tokens=9, output_tokens=3))


def test_keyword_failure_keeps_the_word_match_answer():
    result = run(req("合并 pdf"), KEYWORDS, keywords=Chat(error=RuntimeError("down")))
    assert names(result)[0] == "pdf-merge"
    assert result["degraded"] is True
    stage = next(s for s in result["stages"] if s["stage"] == "keywords")
    assert stage["outcome"] == "failed"


def test_a_slow_keyword_model_stays_inside_the_total_time():
    settings = GuidanceSettings(enabled=True, keywords="local", total_ms=50)
    result = run(req("合并 pdf"), settings, keywords=Chat("x", delay=5))
    assert names(result)[0] == "pdf-merge"
    assert result["degraded"] is True
    assert next(s for s in result["stages"] if s["stage"] == "keywords")["outcome"] == "timeout"


# --- picker ---------------------------------------------------------------------------------


PICKER = GuidanceSettings(enabled=True, picker="external", max_picks=2)
MANY = [asset(f"pdf-{i}", f"pdf tool number {i}") for i in range(5)]


def test_the_picker_chooses_from_the_shortlist():
    chat = Chat('{"picks":[3,1]}')
    result = run(req("pdf", assets=MANY), PICKER, picker=chat)
    assert result["chosen_by"] == "external_model"
    shortlist_text = chat.calls[0][-1]["content"]
    assert len(result["picks"]) == 2
    assert all(p["name"] in shortlist_text for p in result["picks"])


def test_the_picker_cannot_add_anything_outside_the_shortlist():
    result = run(req("pdf", assets=MANY), PICKER, picker=Chat('{"picks":[99,"x",-1,1,1]}'))
    assert len(result["picks"]) == 1


def test_an_empty_picker_answer_is_no_match():
    result = run(req("pdf", assets=MANY), PICKER, picker=Chat('{"picks":[]}'))
    assert result["decision"] == "no_match"
    assert result["degraded"] is False


@pytest.mark.parametrize("text", ["not json", '{"picks":"x"}', "[]"])
def test_a_broken_picker_answer_falls_back_to_the_short_list(text):
    result = run(req("pdf", assets=MANY), PICKER, picker=Chat(text))
    assert result["degraded"] is True
    assert result["chosen_by"] == "word_match"
    assert result["picks"]


def test_a_failed_picker_falls_back_to_the_short_list():
    result = run(req("pdf", assets=MANY), PICKER, picker=Chat(error=RuntimeError("down")))
    assert result["degraded"] is True
    assert result["chosen_by"] == "word_match"
    assert result["picks"]


def test_no_picker_call_is_made_when_nothing_matches():
    chat = Chat('{"picks":[1]}')
    result = run(req("zzzz", assets=MANY), PICKER, picker=chat)
    assert chat.calls == []
    assert result["decision"] == "no_match"


def test_text_sent_to_a_model_is_redacted_first():
    chat = Chat('{"picks":[1]}')
    run(req("pdf secret-value", assets=MANY), PICKER, picker=chat, redact=lambda t: "CLEAN pdf")
    assert "secret-value" not in str(chat.calls)
    assert "CLEAN pdf" in str(chat.calls)


def test_max_picks_in_the_request_wins():
    result = run(req("pdf", assets=MANY, max_picks=1), PICKER, picker=Chat('{"picks":[1,2]}'))
    assert len(result["picks"]) == 1


# --- third party ----------------------------------------------------------------------------


class Selector:
    name, version = "team", "1"
    transport, mechanism = "http", "http"

    def __init__(self, answer=None, error=None, delay=0.0):
        self.answer, self.error, self.delay = answer, error, delay
        self.requests = []

    async def select(self, request, context):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        if callable(self.answer):
            return self.answer(request)
        return self.answer


def ranked(request, wanted=("email-writer",)):
    data = request.data
    by_name = {
        r["identity"].get("package_id") or r["identity"].get("exposed_name"): r
        for r in data["catalog"]
    }
    candidates = [
        dict(
            asset_id=by_name[n]["asset_id"],
            kind=by_name[n]["kind"],
            content_hash=by_name[n]["content_hash"],
            source_version=by_name[n]["source_version"],
            score=1.0,
            score_kind="team",
            evidence_refs=["/description"],
        )
        for n in wanted
    ]
    return dict(
        schema_version=2,
        recommendation_id=data["recommendation_id"],
        catalog_revision=data["catalog_revision"],
        host_snapshot_revision=None,
        selector=dict(name="team", version="1"),
        mechanism="team_rule",
        outcome="ranked",
        decision="choice_required",
        candidates=candidates,
        index_revision=None,
        elapsed_ms=1,
        usage_claims=None,
    )


HTTP = GuidanceSettings(
    enabled=True, primary="http", name="team", version="1", url="http://127.0.0.1:9/select"
)


def test_a_third_party_selector_replaces_the_built_in_steps():
    selector = Selector(ranked)
    result = run(req(), HTTP, selector=selector)
    assert names(result) == ["email-writer"]
    assert result["chosen_by"] == "third_party"
    assert result["mechanism"] == "team_rule"
    data = selector.requests[0].data
    assert data["selection_scope"] == "discovery"
    assert len(data["catalog"]) == 4
    assert data["task_text"] == "please merge these pdf files"


def test_the_offered_record_hash_binds_the_asset_text():
    selector = Selector(ranked)
    run(req(), HTTP, selector=selector)
    record = selector.requests[0].data["catalog"][0]
    assert (
        record["content_hash"] == hashlib.sha256(record["description"].encode("utf-8")).hexdigest()
    )


def test_a_foreign_third_party_pick_is_not_admitted():
    def foreign(request):
        value = ranked(request)
        value["candidates"][0]["asset_id"] = "not-offered"
        return value

    result = run(req(), HTTP, selector=Selector(foreign))
    assert result["degraded"] is True
    assert result["chosen_by"] == "word_match"
    assert names(result)[0] == "pdf-merge"
    assert next(s for s in result["stages"] if s["stage"] == "third_party")["outcome"] == "failed"


def test_a_failed_third_party_falls_back_to_word_match():
    result = run(req(), HTTP, selector=Selector(error=RuntimeError("down")))
    assert result["degraded"] is True
    assert names(result)[0] == "pdf-merge"


def test_a_slow_third_party_stays_inside_the_total_time():
    settings = GuidanceSettings(
        enabled=True,
        primary="http",
        name="team",
        version="1",
        url="http://127.0.0.1:9/select",
        total_ms=50,
    )
    result = run(req(), settings, selector=Selector(ranked, delay=5))
    assert result["degraded"] is True
    assert next(s for s in result["stages"] if s["stage"] == "third_party")["outcome"] == "timeout"


def test_fallback_off_reports_no_match_and_keeps_the_failure_visible():
    settings = GuidanceSettings(
        enabled=True,
        primary="http",
        name="team",
        version="1",
        url="http://127.0.0.1:9/select",
        fallback=False,
    )
    result = run(req(), settings, selector=Selector(error=RuntimeError("down")))
    assert result["decision"] == "no_match"
    assert result["degraded"] is True
    assert result["picks"] == []


def test_a_third_party_gets_the_best_records_above_the_offer_limit():
    many = [asset(f"noise-{i}", "unrelated text here") for i in range(1200)]
    many.append(asset("email-writer", "Write and send an email message"))
    selector = Selector(ranked)
    result = run(req("send an email", assets=many), HTTP, selector=selector)
    assert names(result) == ["email-writer"]
    assert len(selector.requests[0].data["catalog"]) == 1000


# --- bounded catalogs -----------------------------------------------------------------------


def test_a_large_list_is_answered_above_the_recommendation_offer_limit():
    many = [asset(f"noise-{i}", f"unrelated text {i}") for i in range(5000)]
    many.append(asset("seismic-picker", "Pick seismic phases from waveform streams"))
    result = run(req("pick seismic phases", assets=many))
    assert result["offered"] == 5001
    assert names(result)[0] == "seismic-picker"


def test_the_small_local_model_gets_the_shorter_translation_prompt():
    local, external = Chat("merge"), Chat("merge")
    run(req("合并 pdf"), GuidanceSettings(enabled=True, keywords="local"), keywords=local)
    run(req("合并 pdf"), GuidanceSettings(enabled=True, keywords="external"), keywords=external)
    assert local.calls[0][0]["content"].startswith("Translate the user's request")
    assert external.calls[0][0]["content"].startswith("Rewrite the user's request")


# --- review findings ------------------------------------------------------------------------


def test_a_secret_written_in_the_prompt_is_not_sent_to_a_model():
    from relaymind.guidance.pipeline import redact_prompt
    from relaymind.redaction import Redactor

    clean = redact_prompt(Redactor(), "merge pdf api_key: sk-example-secret and password=hunter2")
    assert "sk-example-secret" not in clean
    assert "hunter2" not in clean
    assert "merge pdf" in clean


def test_ordinary_words_near_a_secret_name_stay():
    from relaymind.guidance.pipeline import redact_prompt
    from relaymind.redaction import Redactor

    text = "write about the api key design and token limits"
    assert redact_prompt(Redactor(), text) == text


@pytest.mark.parametrize(
    "usage",
    [
        dict(input_tokens="bad", output_tokens=1),
        dict(input_tokens=-1, output_tokens=None),
        dict(input_tokens=True),
    ],
)
def test_bad_usage_from_a_model_cannot_turn_an_answer_into_an_error(usage):
    result = run(req("pdf", assets=MANY), PICKER, picker=Chat('{"picks":[1]}', usage=usage))
    validate_guidance_result(result)
    assert result["picks"]
    stage = next(s for s in result["stages"] if s["stage"] == "picker")
    assert stage["usage"]["input_tokens"] is None


def test_word_match_stays_inside_the_total_time(monkeypatch):
    import time as time_module

    from relaymind.guidance import pipeline

    def slow(assets, query, tokens=None):
        time_module.sleep(0.3)
        return list(assets)

    monkeypatch.setattr(pipeline, "_rank_now", slow)
    monkeypatch.setattr(pipeline, "_THREAD_ABOVE", -1)
    result = run(req(), GuidanceSettings(enabled=True, total_ms=20))
    assert result["decision"] == "no_match"
    assert result["degraded"] is True
    assert result["stages"][0]["outcome"] == "timeout"
    assert result["stages"][0]["ms"] < 250  # The answer does not wait for the slow ranking.


def test_a_bearer_token_in_the_prompt_is_fully_redacted():
    from relaymind.guidance.pipeline import redact_prompt
    from relaymind.redaction import Redactor

    clean = redact_prompt(Redactor(), "Authorization: Bearer sk-live-123456 then merge pdf")
    assert "sk-live-123456" not in clean
    assert "merge pdf" in clean


def test_a_configured_picker_with_nothing_to_pick_is_reported_as_skipped():
    result = run(req("zzzz", assets=MANY), PICKER, picker=Chat('{"picks":[1]}'))
    assert ("picker", "skipped") in [(s["stage"], s["outcome"]) for s in result["stages"]]


def test_a_keyword_stage_failing_at_its_second_ranking_keeps_its_time_and_usage(monkeypatch):
    from relaymind.guidance import pipeline

    real = pipeline._rank_now
    calls = []

    def fails_second(assets, query, tokens=None):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("ranking failed")
        return real(assets, query, tokens)

    monkeypatch.setattr(pipeline, "_rank_now", fails_second)
    chat = Chat("merge", delay=0.05, usage=dict(input_tokens=3, output_tokens=1))
    result = run(req("合并 pdf"), KEYWORDS, keywords=chat)
    stage = next(s for s in result["stages"] if s["stage"] == "keywords")
    assert len(calls) == 2  # The double ran twice, so the failure is the one this test makes.
    assert result["stages"][0] == dict(result["stages"][0], stage="word_match", outcome="ok")
    assert stage["outcome"] == "failed"
    assert stage["ms"] >= 40
    assert stage["usage"] == dict(input_tokens=3, output_tokens=1)


# --- output limits --------------------------------------------------------------------------


class Limited(Chat):
    """Records the output limit of each call."""

    def __init__(self, text=""):
        super().__init__(text)
        self.limits = []

    async def __call__(self, messages, max_tokens):
        self.limits.append(max_tokens)
        return await super().__call__(messages, max_tokens)


def test_an_external_model_gets_room_to_think_before_it_answers():
    # A real model used 60 output tokens on thinking and returned nothing, on 3 of 40 prompts.
    words = Limited("merge pdf")
    pick = Limited('{"picks":[1]}')
    settings = GuidanceSettings(enabled=True, keywords="external", picker="external")
    run(
        req("请合并这些 pdf", [asset("pdf-merge", "Merge PDF files")]),
        settings,
        keywords=words,
        picker=pick,
    )
    assert words.limits[0] >= 300
    assert pick.limits[0] >= 300


def test_the_small_local_model_keeps_a_short_limit_because_it_does_not_think():
    words = Limited("merge pdf")
    run(req("请合并这些 pdf", [asset("pdf-merge", "Merge PDF files")]), KEYWORDS, keywords=words)
    assert words.limits[0] <= 100


def test_an_empty_keyword_answer_is_a_failed_stage_and_the_word_match_stands():
    result = run(
        req("请合并这些 pdf", [asset("pdf-merge", "Merge PDF files")]),
        KEYWORDS,
        keywords=Chat("   "),
    )
    assert ("keywords", "failed") in [(s["stage"], s["outcome"]) for s in result["stages"]]
    assert result["degraded"] is True
    assert names(result) == ["pdf-merge"]


# --- similar assets (spec 0.69 section 7.7) --------------------------------------------------
OPENING = (
    "Deploy production services in minutes with a guided, repeatable workflow that handles builds, "
    "environment promotion, health checks and rollback. It connects to your repository, keeps "
    "secrets out of build logs and records every change for audit. "
)
SIMILAR = [
    asset("ship-one", OPENING + "Releases run on Amazon ECS with Fargate."),
    asset("ship-two", OPENING + "Releases target Azure Container Apps."),
    asset("ship-three", OPENING + "Releases go to Google Cloud Run."),
]


def picker_listing(chat):
    return chat.calls[0][-1]["content"]


def test_the_picker_sees_what_makes_similar_assets_different():
    chat = Chat('{"picks":[1]}')
    run(req("deploy production services", assets=SIMILAR), PICKER, picker=chat)
    listing = picker_listing(chat)
    for difference in ("Amazon ECS", "Azure Container Apps", "Google Cloud Run"):
        assert difference in listing


def test_the_picker_still_sees_what_a_distinct_asset_does():
    chat = Chat('{"picks":[1]}')
    run(req("pdf", assets=MANY), PICKER, picker=chat)
    assert MANY[0]["description"] in picker_listing(chat)


def test_a_long_description_stays_bounded_for_the_picker():
    long_text = " ".join(f"Sentence number {i} about pdf files." for i in range(50))
    chat = Chat('{"picks":[1]}')
    run(req("pdf", assets=[asset("long", long_text), *SIMILAR]), PICKER, picker=chat)
    line = next(x for x in picker_listing(chat).splitlines() if " long:" in x)
    assert len(line) < 420


def test_a_prompt_that_fits_several_similar_assets_asks_which_one():
    settings = GuidanceSettings(enabled=True, picker="external", max_picks=2, max_choices=3)
    chat = Chat('{"picks":[1,2,3,3,4],"unclear":true}')
    result = run(req("pdf", assets=MANY), settings, picker=chat)
    assert result["decision"] == "choose"
    assert len(result["picks"]) == 3


def test_one_unclear_pick_is_an_ordinary_pick():
    chat = Chat('{"picks":[1],"unclear":true}')
    result = run(req("pdf", assets=MANY), PICKER, picker=chat)
    assert result["decision"] == "picks"


@pytest.mark.parametrize("value", ["true", 1, None])
def test_only_a_real_true_means_unclear(value):
    answer = '{"picks":[1,2,3],"unclear":%s}' % ('"true"' if value == "true" else value or "null")
    result = run(req("pdf", assets=MANY), PICKER, picker=Chat(answer))
    assert result["decision"] == "picks"
    assert len(result["picks"]) == 2


def test_the_picker_is_told_to_report_a_tie():
    chat = Chat('{"picks":[1]}')
    run(req("pdf", assets=MANY), PICKER, picker=chat)
    system = chat.calls[0][0]["content"]
    assert "unclear" in system


@pytest.mark.parametrize("bad", [0, 21, "6", True])
def test_bad_max_choices_stop_startup(bad):
    with pytest.raises(ValueError):
        parse_guidance_settings(dict(max_choices=bad))


def test_a_skill_named_exactly_in_the_prompt_is_always_offered():
    many = [asset(f"pdf-{i}", f"pdf tool number {i} merge split") for i in range(60)]
    many.append(asset("quiet-helper", "Something unrelated"))
    settings = GuidanceSettings(enabled=True, shortlist=5, unpicked_limit=3)
    result = run(req("merge pdf with quiet-helper please", assets=many), settings)
    assert names(result)[0] == "quiet-helper"


NO_SHARED_WORD = [
    asset("replicate-cdc", "Stream database changes to a warehouse with change data capture"),
    asset("pdf-merge", "Merge several pdf documents into one file"),
]


def test_the_picker_gets_a_second_chance_with_keywords_when_it_finds_nothing():
    settings = GuidanceSettings(enabled=True, keywords="external", picker="external")
    keywords = Chat("change data capture database replicate")
    picker = Script('{"picks":[]}', '{"picks":[1]}')
    result = run(
        req("merge orders from the shop into a report", assets=NO_SHARED_WORD),
        settings,
        keywords=keywords,
        picker=picker,
    )
    assert names(result) == ["replicate-cdc"]
    assert len(keywords.calls) == 1
    assert len(picker.calls) == 2


def test_no_second_chance_after_keywords_already_ran():
    settings = GuidanceSettings(enabled=True, keywords="external", picker="external")
    keywords = Chat("pdf merge")
    picker = Script('{"picks":[]}', '{"picks":[1]}')
    result = run(
        req("把文件合并", assets=NO_SHARED_WORD), settings, keywords=keywords, picker=picker
    )
    assert result["decision"] == "no_match"
    assert len(keywords.calls) == 1
    assert len(picker.calls) == 1


def test_no_second_chance_without_a_keyword_stage():
    picker = Script('{"picks":[]}', '{"picks":[1]}')
    result = run(req("merge orders", assets=NO_SHARED_WORD), PICKER, picker=picker)
    assert result["decision"] == "no_match"
    assert len(picker.calls) == 1


def test_several_picks_carry_what_sets_them_apart():
    settings = GuidanceSettings(enabled=True, unpicked_limit=3)
    result = run(req("deploy production services", assets=SIMILAR), settings)
    summaries = {p["name"]: p["summary"] for p in result["picks"]}
    assert "Amazon ECS" in summaries["ship-one"]
    assert "Azure Container Apps" in summaries["ship-two"]
    assert all(len(text) <= 240 for text in summaries.values())


def test_one_pick_is_a_name_only():
    result = run(req("pdf", assets=MANY), PICKER, picker=Chat('{"picks":[1]}'))
    assert list(result["picks"][0]) == ["name", "kind"]


# --- follow-up prompts (spec 0.69 section 7.7) ------------------------------------------------
def test_a_short_follow_up_uses_the_earlier_text_to_find_its_asset():
    assert run(req("yes do that"))["decision"] == "no_match"
    result = run(req("yes do that", recent_text="please merge these pdf files"))
    assert names(result)[0] == "pdf-merge"


def test_a_full_prompt_is_judged_on_its_own_words():
    result = run(req("write an email message to my boss", recent_text="merge these pdf files"))
    assert names(result)[0] == "email-writer"
    assert "pdf-merge" not in names(result)


def test_the_picker_sees_the_earlier_text_as_context():
    chat = Chat('{"picks":[1]}')
    run(req("pdf", assets=MANY, recent_text="the earlier words"), PICKER, picker=chat)
    assert "the earlier words" in chat.calls[0][-1]["content"]


def test_earlier_text_is_redacted_before_a_model_sees_it():
    chat = Chat('{"picks":[1]}')
    run(
        req("pdf", assets=MANY, recent_text="secret-value"),
        PICKER,
        picker=chat,
        redact=lambda text: text.replace("secret-value", "CLEAN"),
    )
    assert "secret-value" not in chat.calls[0][-1]["content"]


def test_too_long_earlier_text_is_refused():
    with pytest.raises(GuidanceError):
        req("pdf", recent_text="x" * 70000)


# --- word match without a picker: fewer, better picks (spec 0.69 section 7.7) ------------------
LARGE = [
    asset(f"filler-{i}", f"common shared words filler number {i} about things") for i in range(60)
]
LARGE += [
    asset("pdf-merge-tool", "Merge pdf documents into one file with bookmarks"),
    asset("pdf-split-tool", "Split pdf documents into pages"),
    asset("weakly-related", "Merge rows of a table"),
]


def test_without_a_picker_a_common_word_alone_picks_nothing_in_a_large_list():
    settings = GuidanceSettings(enabled=True)
    result = run(req("common shared things please", assets=LARGE), settings)
    assert result["decision"] == "no_match"


def test_without_a_picker_much_weaker_matches_are_dropped_in_a_large_list():
    result = run(req("merge pdf documents bookmarks", assets=LARGE))
    assert names(result)[0] == "pdf-merge-tool"
    assert "weakly-related" not in names(result)


def test_a_small_list_keeps_every_match_because_rarity_means_little_there():
    assets = [asset(f"pdf-{i}", f"pdf tool {i}") for i in range(4)]
    assert len(names(run(req("pdf", assets=assets)))) == 4


def test_the_picker_still_sees_the_wide_shortlist_when_word_match_is_unsure():
    chat = Chat('{"picks":[1]}')
    run(req("common shared things please", assets=LARGE), PICKER, picker=chat)
    assert "filler-0" in chat.calls[0][-1]["content"]


# --- second review (spec 0.69 section 7.7) -----------------------------------------------------
def test_a_summary_stays_inside_its_limit_after_joining():
    first = "A" * 59 + "."
    second = "B" * 138 + "."
    third = "C" * 50 + "."
    assets = [
        asset("one", f"{first} {second} {third} word"),
        asset("two", f"{first} {second} {third} word"),
    ]
    result = run(req("word", assets=assets), GuidanceSettings(enabled=True))
    assert all(len(p["summary"]) <= 200 for p in result["picks"])


def test_a_secret_written_in_a_description_is_redacted_before_the_picker_sees_it():
    assets = [asset("deploy", "Deploy pdf here. api_key: sk-secret")]
    chat = Chat('{"picks":[1]}')
    run(
        req("pdf", assets=assets),
        PICKER,
        picker=chat,
        redact=lambda text: text.replace("sk-secret", "CLEAN"),
    )
    assert "sk-secret" not in chat.calls[0][-1]["content"]


def test_a_follow_up_above_the_offer_limit_trims_by_the_earlier_text_too():
    many = [asset(f"filler-{i}", f"filler number {i} yes") for i in range(1000)]
    many.append(asset("weather-tool", "Get the weather forecast"))
    seen = {}

    class Selector:
        async def select(self, request, context):
            seen["ids"] = list(request.offered)
            raise RuntimeError("stop here")

    settings = GuidanceSettings(
        enabled=True, primary="http", name="n", version="1", url="http://127.0.0.1:9/select"
    )
    request = req("yes", assets=many, recent_text="the weather forecast please")
    run(request, settings, selector=Selector())
    assert "host:skill:weather-tool" in seen["ids"]


def test_a_long_prompt_of_repeated_words_is_not_a_follow_up():
    text = "very very very very very very good summary please"
    assert run(req(text, recent_text="merge these pdf files"))["decision"] == "no_match"


def test_a_long_chinese_prompt_is_not_a_follow_up_and_a_short_one_is():
    from relaymind.guidance.pipeline import _word_count

    assert _word_count("继续") == 1
    assert _word_count("好的，就这么做") == 3
    assert _word_count("请帮我把昨天下午整理好的所有报告文件都合并起来") > 6
    assert _word_count("yes, do that") == 3


def test_a_short_new_request_is_not_mixed_with_the_earlier_topic():
    result = run(req("weather forecast", recent_text="please merge these pdf files"))
    assert names(result) == ["weather"]


def test_a_short_new_request_in_a_large_list_is_not_mixed_either():
    assets = [*LARGE, asset("forecast-tool", "Get a weather forecast for a city")]
    result = run(req("weather forecast city", assets=assets, recent_text="merge pdf documents"))
    assert names(result) == ["forecast-tool"]


def test_a_follow_up_with_only_weak_words_in_a_large_list_uses_the_earlier_text():
    result = run(req("common things", assets=LARGE, recent_text="merge pdf documents bookmarks"))
    assert names(result)[0] == "pdf-merge-tool"


def test_keywords_for_a_follow_up_see_the_earlier_text():
    chat = Chat("merge pdf")
    run(req("继续", recent_text="please merge these pdf files"), KEYWORDS, keywords=chat)
    assert "please merge these pdf files" in chat.calls[0][-1]["content"]


def test_keywords_for_a_new_request_with_its_own_match_do_not_see_the_earlier_text():
    chat = Chat("weather forecast")
    assets = [*CATALOG, asset("天气预报工具", "查询天气预报")]
    run(req("查询天气预报", assets=assets, recent_text="old secret topic"), KEYWORDS, keywords=chat)
    assert "old secret topic" not in chat.calls[0][-1]["content"]


# --- big workflows need a set, not one asset (spec 0.69 section 7.7) --------------------------
def test_the_picker_is_told_to_pick_what_each_task_needs_and_not_a_fixed_number():
    chat = Chat('{"picks":[1]}')
    run(req("pdf", assets=MANY), PICKER, picker=chat)
    system = chat.calls[0][0]["content"]
    assert "each separate task" in system
    assert "exactly one" not in system


def test_a_big_workflow_is_not_cut_short_by_the_default_limit():
    chat = Chat('{"picks":[1,2,3,4,5,6,7,8]}')
    assets = [asset(f"pdf-{i}", f"pdf tool number {i}") for i in range(12)]
    settings = GuidanceSettings(enabled=True, picker="external")
    result = run(req("pdf", assets=assets), settings, picker=chat)
    assert len(result["picks"]) == 8
    assert result["decision"] == "picks"


WORKFLOW = [
    asset(f"deploy-{i}", f"deploy release rollout cloud service number {i}") for i in range(60)
]
WORKFLOW += [
    asset("backup-tool", "Backup restore database snapshots"),
    asset("invoice-tool", "Create electronic invoice documents"),
]
WORKFLOW_PROMPT = (
    "Please deploy the release and rollout the cloud service. "
    "Backup the database snapshots. Create the invoice documents."
)


def test_each_task_of_a_workflow_reaches_the_shortlist():
    chat = Chat('{"picks":[1]}')
    settings = GuidanceSettings(enabled=True, picker="external", shortlist=6)
    run(req(WORKFLOW_PROMPT, assets=WORKFLOW), settings, picker=chat)
    listing = chat.calls[0][-1]["content"]
    assert "backup-tool" in listing
    assert "invoice-tool" in listing


def test_without_a_picker_each_task_of_a_workflow_keeps_its_best_match():
    result = run(req(WORKFLOW_PROMPT, assets=WORKFLOW), GuidanceSettings(enabled=True))
    assert {"backup-tool", "invoice-tool"} <= set(names(result))


def test_keywords_written_per_task_each_reach_the_shortlist():
    keywords = Chat(
        "deploy release rollout cloud service\nbackup database snapshots\ninvoice documents"
    )
    picker = Chat('{"picks":[1]}')
    settings = GuidanceSettings(enabled=True, keywords="external", picker="external", shortlist=6)
    run(
        req("把它发布上线，备份数据库，再开发票", assets=WORKFLOW),
        settings,
        keywords=keywords,
        picker=picker,
    )
    listing = picker.calls[0][-1]["content"]
    assert "backup-tool" in listing
    assert "invoice-tool" in listing


def test_the_keyword_writer_is_asked_for_a_line_per_task():
    keywords = Chat("pdf")
    settings = GuidanceSettings(enabled=True, keywords="external")
    run(req("把这些文件合并"), settings, keywords=keywords)
    assert "one line" in keywords.calls[0][0]["content"]


def test_a_one_sentence_prompt_ranks_as_before():
    from relaymind.guidance.pipeline import _task_parts

    assert _task_parts("merge these pdf files") == []
    assert _task_parts("Merge these pdf files now. Then send the email please.") == [
        "Merge these pdf files now.",
        "Then send the email please.",
    ]
    assert _task_parts("Fix it. Merge these pdf files now.") == []


def test_a_failed_second_chance_keeps_the_pickers_answer_that_nothing_fits():
    settings = GuidanceSettings(enabled=True, keywords="external", picker="external")

    class FirstOnly(Chat):
        async def __call__(self, messages, max_tokens):
            self.calls.append(messages)
            if len(self.calls) > 1:
                raise RuntimeError("down")
            return '{"picks":[]}', None

    result = run(
        req("merge orders from the shop into a report", assets=NO_SHARED_WORD),
        settings,
        keywords=Chat("change data capture database replicate"),
        picker=FirstOnly(),
    )
    assert result["decision"] == "no_match"
    assert result["degraded"] is True


# --- review of PR 285 by ArcsinX ---------------------------------------------------------------
def test_text_with_braces_after_the_answer_still_counts():
    answer = '```json\n{"picks":[1],"unclear":false}\n```\nI selected {item 1} because it fits.'
    result = run(req("pdf", assets=MANY), PICKER, picker=Chat(answer))
    assert result["chosen_by"] == "external_model"
    assert names(result) == ["pdf-0"]


def test_an_answer_after_other_braces_still_counts():
    answer = 'Thinking about {the list}. {"picks":[2]}'
    result = run(req("pdf", assets=MANY), PICKER, picker=Chat(answer))
    assert names(result) == ["pdf-1"]


@pytest.mark.parametrize(
    "text",
    [
        "Deploy version 3.14 of the service now.",
        "Deploy the release to github.com today please.",
        "Deploy the release now...backup the database...",
    ],
)
def test_a_dot_inside_a_number_a_domain_or_an_ellipsis_does_not_split_a_task(text):
    from relaymind.guidance.pipeline import _task_parts

    assert _task_parts(text) == []


def test_an_ellipsis_leaves_no_piece_made_only_of_dots():
    from relaymind.guidance.pipeline import _sentences

    assert all(part.strip(". ") for part in _sentences("deploy now... backup db... then report."))


def test_sentences_still_split_where_a_space_follows_and_after_chinese_marks():
    from relaymind.guidance.pipeline import _sentences, _task_parts

    assert _sentences("Runs on v3.14 here. Uses github.com too.") == [
        "Runs on v3.14 here.",
        "Uses github.com too.",
    ]
    assert _task_parts("把文件合并起来吧。再发一封邮件给他们。") == [
        "把文件合并起来吧。",
        "再发一封邮件给他们。",
    ]


def test_a_filler_follow_up_in_a_small_list_still_uses_the_earlier_text():
    assets = [
        asset("pdf-merge", "Merge several pdf documents into one file"),
        asset("todo-list", "Keep a list of things to do and that you finished"),
    ]
    result = run(req("yes do that", assets=assets, recent_text="please merge these pdf files"))
    assert names(result)[0] == "pdf-merge"


# --- second review of PR 285 by ArcsinX -------------------------------------------------------
@pytest.mark.parametrize(
    "text", ["好的，就这么做", "可以，开始吧", "就用这个，谢谢", "继续", "ok, go ahead"]
)
def test_a_chinese_or_english_filler_follow_up_in_a_small_list_uses_the_earlier_text(text):
    assets = [
        asset("pdf-merge", "Merge several pdf documents into one file"),
        asset("zh-helper", "可以把这个文件转换成别的格式，好的结果"),
    ]
    result = run(req(text, assets=assets, recent_text="please merge these pdf files"))
    assert names(result)[0] == "pdf-merge"


def test_a_short_chinese_request_with_its_own_words_is_not_a_follow_up():
    from relaymind.guidance.pipeline import _only_filler

    assert _only_filler("好的，就这么做")
    assert not _only_filler("查询天气预报")
    assert not _only_filler("merge pdf")


def test_a_name_written_next_to_chinese_letters_is_still_named():
    from relaymind.guidance.pipeline import _named_in_prompt

    helper = asset("quiet-helper", "Something unrelated")
    assert _named_in_prompt([helper], "请使用quiet-helper合并文档") == [helper]
    assert _named_in_prompt([helper], "use not-quiet-helper-x please") == []


def test_a_short_polite_sentence_does_not_cancel_the_tasks_of_a_workflow():
    from relaymind.guidance.pipeline import _task_parts

    assert _task_parts("Please convert the PDF file. Then send the email. Thanks.") == [
        "Please convert the PDF file.",
        "Then send the email.",
    ]
    assert _task_parts("把文件合并起来。再发一封邮件给他们。谢谢。") == [
        "把文件合并起来。",
        "再发一封邮件给他们。",
    ]


# --- automated review of PR 285 -----------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        'please use {"api_key": "sk-live-123"} for this',
        "set OPENAI_API_KEY=sk-live-123 and go",
        "the 'api_key' = 'sk-live-123' here",
    ],
)
def test_a_quoted_or_prefixed_secret_is_redacted_before_a_model_sees_it(text):
    from relaymind.guidance.pipeline import redact_prompt
    from relaymind.redaction import Redactor

    cleaned = redact_prompt(Redactor(secret_fields=("api_key",)), text)
    assert "sk-live-123" not in cleaned


def test_a_pick_with_no_description_carries_no_empty_summary():
    assets = [asset("pdf-merge", "Merge pdf documents into one file"), asset("pdf-blank", "")]
    result = run(req("pdf merge", assets=assets), GuidanceSettings(enabled=True))
    by_name = {p["name"]: p for p in result["picks"]}
    assert set(by_name) == {"pdf-merge", "pdf-blank"}
    assert "summary" not in by_name["pdf-blank"]
    assert by_name["pdf-merge"]["summary"]


def test_a_secret_in_a_description_is_redacted_in_the_summaries_too():
    assets = [
        asset("pdf-merge", "Merge pdf documents. Uses api_key: sk-live-123 for the cloud."),
        asset("pdf-split", "Split pdf documents into pages for printing."),
    ]
    result = run(
        req("pdf documents", assets=assets),
        GuidanceSettings(enabled=True),
        redact=lambda text: text.replace("sk-live-123", "[redacted]"),
    )
    summaries = " ".join(p.get("summary", "") for p in result["picks"])
    assert "pdf-merge" in names(result) and "pdf-split" in names(result)
    assert "sk-live-123" not in summaries


def test_a_secret_in_a_description_is_redacted_before_a_third_party_selector_sees_it():
    seen = {}

    class Selector:
        async def select(self, request, context):
            seen["descriptions"] = [r["description"] for r in request.offered.values()]
            raise RuntimeError("stop here")

    settings = GuidanceSettings(
        enabled=True, primary="http", name="n", version="1", url="http://127.0.0.1:9/select"
    )
    assets = [asset("pdf-merge", "Merge pdf files. api_key: sk-live-123")]
    run(
        req("pdf", assets=assets),
        settings,
        selector=Selector(),
        redact=lambda text: text.replace("sk-live-123", "[redacted]"),
    )
    assert seen["descriptions"] and all("sk-live-123" not in d for d in seen["descriptions"])


def test_a_slow_third_party_still_leaves_time_for_the_word_match_answer():
    class Slow:
        async def select(self, request, context):
            await asyncio.sleep(5)

    settings = GuidanceSettings(
        enabled=True,
        primary="http",
        name="n",
        version="1",
        url="http://127.0.0.1:9/select",
        total_ms=400,
    )
    result = run(req(), settings, selector=Slow())
    assert names(result)[0] == "pdf-merge"
    assert result["chosen_by"] == "word_match"
    assert result["degraded"] is True


def test_an_explicit_max_picks_caps_an_undecided_list_too():
    settings = GuidanceSettings(enabled=True, picker="external", max_choices=6)
    chat = Chat('{"picks":[1,2,3,4],"unclear":true}')
    result = run(req("pdf", assets=MANY, max_picks=2), settings, picker=chat)
    assert result["decision"] == "choose"
    assert len(result["picks"]) == 2


def test_without_an_explicit_max_picks_an_undecided_list_reaches_max_choices():
    settings = GuidanceSettings(enabled=True, picker="external", max_picks=2, max_choices=4)
    chat = Chat('{"picks":[1,2,3,4,5],"unclear":true}')
    result = run(req("pdf", assets=MANY), settings, picker=chat)
    assert len(result["picks"]) == 4


def test_a_follow_up_whose_second_ranking_times_out_keeps_the_first_answer(monkeypatch):
    from relaymind.guidance import pipeline

    real = pipeline._rank
    calls = []

    async def slow_second(assets, query, tokens=None):
        # The first ranking goes through _rank_tasks; only the follow-up's wider one comes here.
        calls.append(query)
        await asyncio.sleep(5)
        return await real(assets, query, tokens)

    monkeypatch.setattr(pipeline, "_rank", slow_second)
    assets = [
        asset("pdf-merge", "Merge several pdf documents into one file"),
        asset("todo-list", "Keep a list of things to do and that you finished"),
    ]
    settings = GuidanceSettings(enabled=True, total_ms=300)
    result = run(req("yes do that", assets=assets, recent_text="merge pdf files"), settings)
    assert len(calls) == 1
    assert names(result) == ["todo-list"]
    assert result["degraded"] is True
    # One word_match stage, which says why it is degraded, not a failed one and an ok one.
    stages = [s for s in result["stages"] if s["stage"] == "word_match"]
    assert [s["outcome"] for s in stages] == ["timeout"]


# --- third review of PR 285 -----------------------------------------------------------------------
@pytest.mark.parametrize("answer", ['{"picks":["pdf-1"]}', '{"picks":["1"]}', '{"picks":[99]}'])
def test_a_picker_answer_that_names_no_listed_number_is_a_failed_stage(answer):
    result = run(req("pdf", assets=MANY), PICKER, picker=Chat(answer))
    assert result["degraded"] is True
    assert result["chosen_by"] == "word_match"
    assert names(result)


def test_an_undecided_list_is_capped_by_max_choices_even_below_max_picks():
    settings = GuidanceSettings(enabled=True, picker="external", max_picks=8, max_choices=3)
    chat = Chat('{"picks":[1,2,3,4,5],"unclear":true}')
    result = run(req("pdf", assets=MANY), settings, picker=chat)
    assert result["decision"] == "choose"
    assert len(result["picks"]) == 3
    assert "at most 3" in chat.calls[0][0]["content"]


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("use quiet-helper.", True),
        ("use quiet-helper: now", True),
        ("use quiet-helper.v2 here", False),
        ("use quiet-helper:extra here", False),
        ("use x.quiet-helper here", False),
    ],
)
def test_a_name_is_whole_only_when_no_separator_and_letter_continue_it(text, found):
    from relaymind.guidance.pipeline import _named_in_prompt

    helper = asset("quiet-helper", "Something unrelated")
    assert bool(_named_in_prompt([helper], text)) is found


def test_a_secret_written_right_after_chinese_letters_is_redacted():
    from relaymind.guidance.pipeline import redact_prompt
    from relaymind.redaction import Redactor

    cleaned = redact_prompt(Redactor(secret_fields=("api_key",)), "配置api_key: sk-live-123 然后")
    assert "sk-live-123" not in cleaned
    assert redact_prompt(Redactor(secret_fields=("api_key",)), "myapi_key: x") == "myapi_key: x"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("配置api_key：sk-live-123456", "配置api_key："),
        ("配置api_key＝sk-live-123456", "配置api_key＝"),
        ("api_key: sk-live-123456，然后帮我合并pdf", "，然后帮我合并pdf"),
        ("api_key: sk-live-123456。再发邮件", "。再发邮件"),
    ],
)
def test_a_secret_with_chinese_punctuation_is_redacted_and_the_rest_stays(text, kept):
    from relaymind.guidance.pipeline import redact_prompt
    from relaymind.redaction import Redactor

    cleaned = redact_prompt(Redactor(secret_fields=("api_key",)), text)
    assert "sk-live-123456" not in cleaned
    assert kept in cleaned


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("配置api_key: bearer sk-live-123456，然后帮我合并pdf", "，然后帮我合并pdf"),
        ("api_key=sk-live-123456;next_step()", ";next_step()"),
        ("(api_key: sk-live-123456) continue", ") continue"),
        ("（api_key：sk-live-123456）继续", "）继续"),
        ("【api_key：sk-live-123456】继续", "】继续"),
        ("“api_key：sk-live-123456”继续", "”继续"),
    ],
)
def test_a_secret_value_ends_at_punctuation_even_after_bearer(text, kept):
    from relaymind.guidance.pipeline import redact_prompt
    from relaymind.redaction import Redactor

    cleaned = redact_prompt(Redactor(secret_fields=("api_key",)), text)
    assert "sk-live-123456" not in cleaned
    assert kept in cleaned


def test_an_explicit_max_picks_caps_the_unpicked_list():
    assets = [asset(f"pdf-tool-{n}", f"merge pdf files way {n}") for n in range(6)]
    result = run(req("merge pdf files", assets=assets, max_picks=2), GuidanceSettings(enabled=True))
    assert result["chosen_by"] == "word_match"
    assert len(result["picks"]) == 2


# --- self-review before the next round of PR 285 ----------------------------------------------


def test_a_chinese_prompt_of_several_sentences_keeps_its_keyword_matches():
    """Keywords written on one line apply to the whole prompt when no task matches alone."""
    assets = [
        asset("disk-check", "Check disk usage on a server"),
        asset("mail-send", "Send an email message to a team"),
    ]
    chat = Chat("check disk usage server send email message")
    result = run(
        req("请检查服务器的磁盘使用情况。然后给团队发送一封邮件。", assets=assets),
        KEYWORDS,
        keywords=chat,
    )
    assert set(names(result)) == {"disk-check", "mail-send"}


def test_tokenizing_a_large_list_does_not_hold_the_event_loop(monkeypatch):
    import threading

    from relaymind.guidance import pipeline

    seen = []
    real = pipeline._tokens

    def where(item):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(item)

    monkeypatch.setattr(pipeline, "_tokens", where)
    assets = [asset(f"tool-{n}", f"merge pdf files way {n}") for n in range(600)]
    run(req("merge pdf files. then send them.", assets=assets), GuidanceSettings(enabled=True))
    assert seen and not any(seen)


def test_a_deeply_nested_picker_answer_is_a_failed_stage_not_an_error():
    chat = Chat('{"picks":' + "[" * 100_000)
    result = run(req("pdf", assets=MANY), PICKER, picker=chat)
    assert result["degraded"] is True
    assert ("picker", "failed") in [(s["stage"], s["outcome"]) for s in result["stages"]]


@pytest.mark.parametrize(
    "text",
    [
        "x-api-key: sk-live-123456",
        "API-KEY: sk-live-123456",
        '{"apiKey": "sk-live-123456"}',
        "ApiKey=sk-live-123456",
        '{"password": "ab\\"cdsk-live-123456"}',
    ],
)
def test_field_name_spellings_and_escaped_quotes_are_redacted(text):
    from relaymind.guidance.pipeline import redact_prompt
    from relaymind.redaction import Redactor

    assert "sk-live-123456" not in redact_prompt(Redactor(), text)


def test_a_failed_third_party_falls_back_to_word_match_only():
    """Spec 7.7: a selector failure falls back to stage 1, with no model call."""
    settings = GuidanceSettings(
        enabled=True,
        primary="http",
        name="n",
        version="1",
        url="http://127.0.0.1:9/select",
        keywords="external",
        picker="external",
    )
    chat = Chat('{"picks":[1]}')
    result = run(
        req("把 pdf 文件合并"),
        settings,
        selector=Selector(error=RuntimeError("down")),
        keywords=chat,
        picker=chat,
    )
    assert chat.calls == []
    assert result["chosen_by"] == "word_match"
    assert {s["stage"] for s in result["stages"]} == {"third_party", "word_match"}


@pytest.mark.parametrize("bad", [dict(name=1), dict(version=True), dict(name="  ")])
def test_a_selector_name_and_version_must_be_text(bad):
    raw = dict(primary="http", name="n", version="1", url="http://127.0.0.1:9/select") | bad
    with pytest.raises(ValueError):
        parse_guidance_settings(raw)


def test_a_blank_task_is_an_invalid_request():
    with pytest.raises(GuidanceError):
        req("   \n ")


def test_a_redacted_prompt_still_fits_the_third_party_request():
    seen = {}

    def answer(request):
        seen["text"] = request.data["task_text"]
        raise RuntimeError("stop")

    text = ("token=x " * 1024)[:8190]
    run(
        req(text),
        HTTP,
        selector=Selector(answer=answer),
        redact=lambda value: value.replace("token=x", "token=[REDACTED]"),
    )
    assert "text" in seen and len(seen["text"].encode("utf-8")) <= 8192


def test_the_launcher_redacts_what_the_guidance_models_see(monkeypatch):
    from types import SimpleNamespace

    from relaymind import launcher
    from relaymind.api import guidance as api
    from relaymind.redaction import Redactor

    captured = {}
    monkeypatch.setattr(api, "install_guidance_routes", lambda app, s, **kw: captured.update(kw))
    config = SimpleNamespace(guidance=GuidanceSettings(enabled=True), local_url=None, external=None)
    launcher._install_guidance(object(), config, SimpleNamespace(redactor=Redactor()))
    assert captured["redact"]("api_key: sk-live-123456") == "api_key: [redacted]"


def test_a_redacted_summary_stays_inside_its_limit():
    from relaymind.guidance.pipeline import redact_prompt
    from relaymind.redaction import Redactor

    long = "Sends mail to people. " + " ".join(f"Set token=a{n}." for n in range(40))
    assets = [asset("mail-a", long), asset("mail-b", "Sends mail to people in a team.")]
    result = run(
        req("send mail to people", assets=assets),
        GuidanceSettings(enabled=True),
        redact=lambda text: redact_prompt(Redactor(), text),
    )
    assert all(len(p.get("summary", "")) <= 200 for p in result["picks"])


def test_a_long_recent_text_keeps_its_end_for_a_third_party_selector():
    seen = {}

    def answer(request):
        seen["recent"] = request.data["conversation"][0]["text"]
        raise RuntimeError("stop")

    recent = "x" * 9000 + " merge the pdf files"
    run(
        req("yes do that", recent_text=recent[-8000:]),
        HTTP,
        selector=Selector(answer=answer),
        redact=lambda text: text.replace("x", "xx"),
    )
    assert seen["recent"].endswith("merge the pdf files")


@pytest.mark.parametrize(
    "description",
    ["Edit <b>PDF</b> files", "Edit pdf\x01 files", "Edit pdf files\n" + "same line\n" * 5],
)
def test_one_odd_description_does_not_stop_the_third_party_selector(description):
    seen = {}

    def answer(request):
        seen["names"] = [r["identity"].get("package_id") for r in request.data["catalog"]]
        raise RuntimeError("stop")

    assets = [asset("pdf-edit", description), asset("pdf-merge", "Merge pdf files")]
    run(req("edit pdf files", assets=assets), HTTP, selector=Selector(answer=answer))
    assert set(seen["names"]) == {"pdf-edit", "pdf-merge"}


def test_a_secret_split_by_a_sentence_end_is_redacted_in_the_listing():
    from relaymind.guidance.pipeline import redact_prompt
    from relaymind.redaction import Redactor

    long = (
        "Merge pdf files for a team. It keeps the page order. "
        'password: "correct horse. battery staple" is read from the config.'
    )
    chat = Chat('{"picks":[1,2]}')
    run(
        req("merge pdf files", assets=[asset("pdf-a", long), asset("pdf-b", "Merge pdf pages")]),
        GuidanceSettings(enabled=True, picker="external"),
        picker=chat,
        redact=lambda text: redact_prompt(Redactor(), text),
    )
    assert "battery staple" not in chat.calls[0][-1]["content"]


def test_the_word_match_shortlist_count_is_capped_by_the_setting():
    assets = [asset(f"pdf-{n}", f"pdf tool {n}") for n in range(30)]
    result = run(req("pdf", assets=assets), GuidanceSettings(enabled=True, shortlist=5))
    assert result["shortlist"] == 5


def test_the_token_cache_is_keyed_by_a_digest_not_the_text():
    from relaymind.guidance import pipeline

    pipeline._token_cache.clear()
    pipeline._tokens(dict(name="pdf-merge", description="Merge pdf files " * 50))
    (key,) = pipeline._token_cache
    assert isinstance(key, bytes) and len(key) <= 32


def test_one_stage_name_appears_once_even_when_it_runs_again():
    """A keyword stage skipped and then run, and a picker asked twice, are one entry each."""
    picker = Chat('{"picks":[]}')
    keywords = Chat("send mail")  # Widens the shortlist, so the picker is asked again.
    settings = GuidanceSettings(enabled=True, keywords="external", picker="external")
    assets = [asset("pdf-merge", "Merge pdf documents"), asset("mail", "Send mail")]
    result = run(req("merge my files", assets=assets), settings, keywords=keywords, picker=picker)
    names = [s["stage"] for s in result["stages"]]
    assert len(names) == len(set(names))
    assert len(picker.calls) == 2


def test_a_prompt_that_matches_no_word_gets_keywords_before_the_picker():
    """Spec 7.7: a keyword stage that has not run runs, and the picker gets its call."""
    keywords = Chat("merge pdf documents")
    picker = Chat('{"picks":[1]}')
    settings = GuidanceSettings(enabled=True, keywords="external", picker="external")
    assets = [asset("pdf-merge", "Merge pdf documents"), asset("mail", "Send mail")]
    result = run(
        req("combine my reports", assets=assets), settings, keywords=keywords, picker=picker
    )
    assert len(keywords.calls) == 1 and len(picker.calls) == 1
    assert names(result) == ["pdf-merge"]


@pytest.mark.parametrize(
    ("first", "second", "merged"),
    [
        (None, dict(input_tokens=5, output_tokens=1), None),
        (
            dict(input_tokens=None, output_tokens=None),
            dict(input_tokens=None, output_tokens=None),
            dict(input_tokens=None, output_tokens=None),
        ),
        (
            dict(input_tokens=3, output_tokens=1),
            dict(input_tokens=5, output_tokens=None),
            dict(input_tokens=8, output_tokens=None),
        ),
    ],
)
def test_usage_of_a_stage_run_twice_is_never_made_up(first, second, merged):
    """Unknown stays unknown: a sum with an unknown part is not a smaller known number."""
    from relaymind.guidance.pipeline import _Run

    stage = _Run(GuidanceSettings(enabled=True), lambda: 0.0, lambda t: t)
    stage.record("picker", "failed", 0.0, first)
    stage.record("picker", "ok", 0.0, second)
    assert stage.stages[0]["usage"] == merged
