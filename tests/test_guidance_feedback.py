"""Selector feedback through the guidance endpoints (RM48, AC43; spec 0.75 section 7.7).

After a turn, the host plugin tells the gateway which offered assets the turn used, and the gateway
passes them on to the selector as pairs of that select. A fake router answers select, sync and
feedback on loopback; each test reads what the router received.
"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from launcher_helpers import write_config
from relaymind.api.guidance import install_guidance_routes
from relaymind.guidance import GuidanceSettings
from relaymind.guidance import feedback as feedback_module
from relaymind.guidance.feedback import GuidanceFeedback
from relaymind.guidance.sync import GuidanceSync
from relaymind.launcher import LaunchError, load_config
from relaymind.schema_util import StrictValidator, first_schema_error, load_packaged_schema
from relaymind.selection.asset_http import HttpAssetSelector
from test_guidance_sync import (
    AUTH,
    BODY,
    HTTP_TABLE,
    SELECT_URL,
    SKILL,
    SKILL_HASH,
    SYNC_URL,
    SYNCING,
    TOOL,
    Clock,
    Router,
    pairs,
    reply,
)

FEEDBACK_URL = "http://127.0.0.1:8940/feedback"
FEEDING = replace(SYNCING, feedback_url=FEEDBACK_URL)
STATUS = StrictValidator(load_packaged_schema("relaymind.guidance", "guidance_sync_status.v1.json"))
USED_SKILL = dict(name="pdf-merge", kind="skill", signal="loaded")
USED_TOOL = dict(name="weather", kind="mcp_tool", signal="executed")


def client(router: Router, settings=FEEDING, clock=None):
    app = FastAPI()
    app.state.application_token = "token"
    selector = HttpAssetSelector(
        name="router", version="1", url=SELECT_URL, transport_factory=router.transport
    )
    sync = feedback = None
    extra = {} if clock is None else dict(clock=clock)
    if settings.sync_url:
        sync = GuidanceSync(settings, transport_factory=router.transport, **extra)
    if settings.feedback_url:
        feedback = GuidanceFeedback(settings, sync, transport_factory=router.transport, **extra)
    install_guidance_routes(app, settings, selector=selector, sync=sync, feedback=feedback)
    return TestClient(app)


def ask(http) -> dict:
    answer = http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert answer.status_code == 200
    return answer.json()


def tell(http, recommendation_id, used) -> httpx.Response:
    body = dict(schema_version=1, recommendation_id=recommendation_id, used=used)
    return http.post("/relay/v1/guidance/feedback", json=body, headers=AUTH)


def status(http) -> dict:
    body = http.get("/relay/v1/guidance/sync", headers=AUTH).json()
    assert first_schema_error(STATUS, body) is None
    return body


def feedback_counts(http) -> dict:
    return {k: v for k, v in status(http).items() if k.startswith("feedback_")}


@pytest.mark.parametrize(
    "table",
    [
        HTTP_TABLE + f"feedback_url = '{FEEDBACK_URL}'\n",
        HTTP_TABLE + f"sync_url = '{SYNC_URL}'\nfeedback_url = 'http://example.com/feedback'\n",
        HTTP_TABLE + f"sync_url = '{SYNC_URL}'\nfeedback_url = 7\n",
    ],
)
def test_bad_feedback_settings_stop_startup(tmp_path, table):
    """A feedback URL needs selector sync and a loopback address."""
    with pytest.raises(LaunchError):
        load_config(write_config(tmp_path, table))


def test_without_a_feedback_url_no_answer_has_an_id_and_nothing_is_queued():
    router = Router()
    http = client(router, SYNCING)
    assert "recommendation_id" not in ask(http)
    answer = tell(http, "any", [USED_SKILL])
    assert answer.status_code == 200 and answer.json() == dict(schema_version=1, queued=False)
    assert router.feedbacks == []
    assert feedback_counts(http) == dict(
        feedback_enabled=False, feedback_sent=0, feedback_failed=0, feedback_skipped=0
    )


def test_used_assets_reach_the_selector_as_pairs_of_that_select_picked_or_not():
    """The tool was not picked, yet the turn called it: feedback names it too (RM48)."""
    router = Router()
    http = client(router)
    answer = ask(http)
    select = router.selects[0]
    assert answer["recommendation_id"] == select["recommendation_id"]
    assert [p["name"] for p in answer["picks"]] == ["pdf-merge"]
    told = tell(http, answer["recommendation_id"], [USED_SKILL, USED_TOOL])
    assert told.json() == dict(schema_version=1, queued=True)
    hashes = {r["asset_id"]: r["content_hash"] for r in select["catalog"]}
    assert router.feedbacks == [
        dict(
            schema_version=1,
            operation="feedback",
            feeder_id="guidance",
            recommendation_id=select["recommendation_id"],
            used=[
                dict(
                    asset_id="host:skill:pdf-merge",
                    content_hash=SKILL_HASH,
                    kind="skill",
                    signal="loaded",
                ),
                dict(
                    asset_id="host:mcp_tool:weather",
                    content_hash=hashes["host:mcp_tool:weather"],
                    kind="mcp_tool",
                    signal="executed",
                ),
            ],
        )
    ]
    # No prompt text and no path ever goes with feedback.
    sent = json.dumps(router.feedbacks)
    assert "merge these" not in sent and SKILL["path"] not in sent
    assert feedback_counts(http)["feedback_sent"] == 1


def test_an_unknown_inference_is_asked_with_find_missing_that_uploads_nothing():
    router = Router(missing=lambda sent: sent)
    http = client(router)
    tell(http, ask(http)["recommendation_id"], [USED_SKILL])
    assert router.syncs == [
        dict(
            schema_version=1,
            operation="find_missing",
            feeder_id="guidance",
            assets=pairs(router.selects[0]),
        )
    ]
    assert len(router.feedbacks) == 1
    # Only a sync reports sync counts; asking the inference is not one.
    assert status(http)["last_outcome"] == "none"


def test_an_invalid_find_missing_answer_teaches_no_inference_and_fails_the_feedback():
    """A missing pair that was not offered makes the answer invalid, from a sync or a question."""
    stray = [dict(asset_id="host:skill:other", content_hash="2" * 64)]
    router = Router(outcome="not_ready", missing=lambda sent: stray)
    clock = Clock()
    http = client(router, clock=clock)
    recommendation_id = ask(http)["recommendation_id"]
    assert [s["operation"] for s in router.syncs] == ["find_missing"]
    clock.now += 31  # Past the wait the failed sync started.
    tell(http, recommendation_id, [USED_SKILL])
    assert [s["operation"] for s in router.syncs] == ["find_missing", "find_missing"]
    assert router.feedbacks == []
    assert feedback_counts(http)["feedback_failed"] == 1


def test_the_inference_question_waits_like_a_sync_after_a_failure():
    """Review of PR 306: the question shares the sync's one slot and its wait (spec 0.75)."""
    router = Router(outcome="not_ready", sync_status=500)
    clock = Clock()
    http = client(router, clock=clock)
    tell(http, ask(http)["recommendation_id"], [USED_SKILL])
    # The sync failed and its wait has not ended: no question, and the feedback is skipped.
    assert [s["operation"] for s in router.syncs] == ["find_missing"]
    assert feedback_counts(http)["feedback_skipped"] == 1


def test_a_failed_inference_question_starts_the_wait():
    router = Router(sync_status=500)
    http = client(router)
    tell(http, ask(http)["recommendation_id"], [USED_SKILL])
    tell(http, ask(http)["recommendation_id"], [USED_SKILL])
    assert [s["operation"] for s in router.syncs] == ["find_missing"]
    counts = feedback_counts(http)
    assert counts["feedback_failed"] == 1 and counts["feedback_skipped"] == 1
    body = status(http)
    assert body["consecutive_failures"] == 1 and body["retry_after_ms"] > 0
    # A question is not a sync, so the sync counts stay those of no sync.
    assert body["last_outcome"] == "none"


def test_with_remote_allowed_the_inference_is_not_asked():
    router = Router(inference="remote")
    http = client(router, replace(FEEDING, sync_allow_remote=True))
    tell(http, ask(http)["recommendation_id"], [USED_SKILL])
    assert router.syncs == [] and len(router.feedbacks) == 1


def test_a_known_inference_is_not_asked_again():
    router = Router()
    http = client(router)
    for _ in range(2):
        tell(http, ask(http)["recommendation_id"], [USED_SKILL])
    assert [s["operation"] for s in router.syncs] == ["find_missing"]
    assert len(router.feedbacks) == 2


def test_an_empty_list_is_feedback_too():
    router = Router()
    http = client(router)
    assert tell(http, ask(http)["recommendation_id"], []).json()["queued"] is True
    assert router.feedbacks[0]["used"] == []


def test_an_asset_the_select_did_not_offer_is_left_out():
    router = Router()
    http = client(router)
    other = dict(name="not-offered", kind="skill", signal="loaded")
    tell(http, ask(http)["recommendation_id"], [other, USED_SKILL])
    assert [u["asset_id"] for u in router.feedbacks[0]["used"]] == ["host:skill:pdf-merge"]


def test_a_select_gets_at_most_one_feedback_and_an_unknown_id_none():
    router = Router()
    http = client(router)
    recommendation_id = ask(http)["recommendation_id"]
    assert tell(http, recommendation_id, [USED_SKILL]).json()["queued"] is True
    assert tell(http, recommendation_id, [USED_SKILL]).json()["queued"] is False
    assert tell(http, "7e46678a-d704-4027-b916-2c84de864101", []).json()["queued"] is False
    assert len(router.feedbacks) == 1


def test_an_id_is_forgotten_after_an_hour():
    router = Router()
    clock = Clock()
    http = client(router, clock=clock)
    recommendation_id = ask(http)["recommendation_id"]
    clock.now += 3601
    assert tell(http, recommendation_id, [USED_SKILL]).json()["queued"] is False
    assert router.feedbacks == []


def test_a_not_ready_answer_has_an_id_and_its_feedback_is_sent():
    """The selector got the request even when it had not indexed the offer."""
    router = Router(outcome="not_ready")
    http = client(router)
    answer = ask(http)
    assert answer["chosen_by"] == "word_match" and answer["degraded"] is True
    assert answer["recommendation_id"] == router.selects[0]["recommendation_id"]
    assert tell(http, answer["recommendation_id"], [USED_SKILL]).json()["queued"] is True
    assert len(router.feedbacks) == 1


def test_a_failed_select_has_no_id():
    router = Router(select_status=500)
    answer = ask(client(router))
    assert answer["degraded"] is True and "recommendation_id" not in answer


def test_a_selector_with_remote_inference_gets_no_feedback_unless_the_operator_allows_it():
    router = Router(inference="remote")
    http = client(router)
    tell(http, ask(http)["recommendation_id"], [USED_SKILL])
    assert router.feedbacks == []
    assert feedback_counts(http)["feedback_skipped"] == 1
    allowed = client(router, replace(FEEDING, sync_allow_remote=True))
    tell(allowed, ask(allowed)["recommendation_id"], [USED_SKILL])
    assert len(router.feedbacks) == 1


def test_a_feedback_answer_saying_remote_stops_later_feedback():
    """The last inference any exchange reported decides, a feedback answer's included."""
    router = Router()
    http = client(router)
    first = ask(http)["recommendation_id"]
    second = ask(http)["recommendation_id"]
    tell(http, first, [])
    router.inference = "remote"
    tell(http, first, [])  # Already taken: not sent, and nothing learned.
    tell(http, ask(http)["recommendation_id"], [])
    assert len(router.feedbacks) == 2
    tell(http, second, [])
    assert len(router.feedbacks) == 2 and feedback_counts(http)["feedback_skipped"] == 1


@pytest.mark.parametrize("where", ["feedback", "inference"])
def test_a_failed_feedback_is_counted_and_changes_nothing(where):
    router = Router(**({"feedback_status": 500} if where == "feedback" else {"sync_status": 500}))
    http = client(router)
    answer = tell(http, ask(http)["recommendation_id"], [USED_SKILL])
    assert answer.json()["queued"] is True
    assert feedback_counts(http) == dict(
        feedback_enabled=True, feedback_sent=0, feedback_failed=1, feedback_skipped=0
    )
    assert ask(http)["chosen_by"] == "third_party"


@pytest.mark.parametrize(
    "body",
    [
        dict(schema_version=1, recommendation_id="x", used=[dict(USED_SKILL, signal="executed")]),
        dict(schema_version=1, recommendation_id="x", used=[dict(USED_TOOL, signal="loaded")]),
        dict(schema_version=1, recommendation_id="x", used=[USED_SKILL, USED_SKILL]),
        dict(schema_version=1, recommendation_id="x", used=[dict(USED_SKILL, path="/x")]),
        dict(schema_version=1, recommendation_id="x", used=[USED_SKILL] * 1001),
        dict(schema_version=1, recommendation_id="x", used=[dict(USED_SKILL, name="é" * 65)]),
        dict(schema_version=1, recommendation_id="", used=[]),
        dict(schema_version=1, recommendation_id="x", used=[], task_text="merge these"),
        dict(schema_version=2, recommendation_id="x", used=[]),
    ],
)
def test_an_invalid_feedback_is_refused(body):
    answer = client(Router()).post("/relay/v1/guidance/feedback", json=body, headers=AUTH)
    assert answer.status_code == 400
    assert answer.json()["error"]["code"] == "invalid_request"


def test_a_feedback_body_over_one_mib_is_refused():
    body = b'{"schema_version":1,"recommendation_id":"x","used":[]' + b" " * (1024 * 1024) + b"}"
    answer = client(Router()).post(
        "/relay/v1/guidance/feedback",
        content=body,
        headers=dict(AUTH, **{"Content-Type": "application/json"}),
    )
    assert answer.status_code == 400


def test_the_feedback_route_needs_the_token_and_guidance():
    assert client(Router()).post("/relay/v1/guidance/feedback", json={}).status_code == 401
    off = client(Router(), replace(FEEDING, enabled=False))
    answer = tell(off, "x", [])
    assert answer.status_code == 503
    assert answer.json()["error"]["code"] == "recommendation_unavailable"


def test_an_offer_dropped_from_memory_forgets_its_ids(monkeypatch):
    """Each distinct offer is kept once, at most MAX_OFFERS of them (spec 0.75)."""
    monkeypatch.setattr(feedback_module, "MAX_OFFERS", 1)
    router = Router()
    http = client(router)
    first = ask(http)["recommendation_id"]
    again = ask(http)["recommendation_id"]  # The same offer is kept once for both.
    http.post("/relay/v1/guidance", json=dict(BODY, assets=[SKILL]), headers=AUTH)
    assert tell(http, first, []).json()["queued"] is False
    assert tell(http, again, []).json()["queued"] is False


def test_the_ids_are_capped_with_the_oldest_dropped_first(monkeypatch):
    monkeypatch.setattr(feedback_module, "MAX_IDS", 2)
    router = Router()
    http = client(router)
    ids = [ask(http)["recommendation_id"] for _ in range(3)]
    assert [tell(http, i, []).json()["queued"] for i in ids] == [False, True, True]


def test_one_feedback_is_sent_at_a_time_and_a_full_queue_drops_one():
    """At most MAX_WAITING feedbacks wait behind the one being sent (spec 0.75)."""
    release = asyncio.Event()
    sent = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["operation"] == "feedback":
            sent.append(body["recommendation_id"])
            await release.wait()
        return reply(
            dict(
                schema_version=1,
                operation=body["operation"],
                inference="local",
                missing=[] if body["operation"] == "find_missing" else None,
                results=None,
            )
        )

    async def scenario():
        sync = GuidanceSync(FEEDING, transport_factory=lambda: httpx.MockTransport(handler))
        feedback = GuidanceFeedback(
            FEEDING, sync, transport_factory=lambda: httpx.MockTransport(handler)
        )
        catalog = [
            dict(asset_id="host:skill:a", kind="skill", content_hash=SKILL_HASH, description="a")
        ]
        jobs = []
        for i in range(feedback_module.MAX_WAITING + 2):
            feedback.remember(f"id-{i}", 1, catalog)
            jobs.append(feedback.take(dict(schema_version=1, recommendation_id=f"id-{i}", used=[])))
        tasks = [asyncio.create_task(feedback.send(job)) for job in jobs]
        for _ in range(1000):  # Bounded, so a failing exchange fails the test instead of hanging.
            if sent:
                break
            await asyncio.sleep(0.001)
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(sent) == 1 and feedback.status()["feedback_skipped"] == 1
        release.set()
        await asyncio.gather(*tasks)
        return feedback.status()

    counts = asyncio.run(scenario())
    assert counts["feedback_sent"] == feedback_module.MAX_WAITING + 1
    assert counts["feedback_skipped"] == 1 and len(sent) == feedback_module.MAX_WAITING + 1


def test_the_launched_gateway_sends_feedback_to_the_reference_selector(tmp_path):
    """AC43: the launcher wires feedback_url; the reference selector serves /feedback."""
    import threading
    from http.server import ThreadingHTTPServer

    from helpers import ScriptedAdapter, load_script_module
    from relaymind.launcher import build_app

    server = load_script_module("guidance_selector_server")
    index = server.SyncIndex([tmp_path])
    settings = GuidanceSettings(enabled=True)

    def answer(value):
        return asyncio.run(server.select(value, settings, name="router", version="1"))

    handler = server.make_handler(answer, index.handle, index.handle)
    http_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=http_server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http_server.server_port}"
    table = (
        "\n[guidance]\nenabled = true\nprimary = 'http'\nname = 'router'\nversion = '1'\n"
        f"url = '{base}/select'\nsync_url = '{base}/sync'\nfeedback_url = '{base}/feedback'\n"
    )
    try:
        config = load_config(write_config(tmp_path, table))
        launched = build_app(config, local_adapter=ScriptedAdapter(turns=[]), external_adapter=None)
        auth = {"Authorization": f"Bearer {launched.app.state.application_token}"}
        with TestClient(launched.app) as http:
            body = dict(
                BODY, assets=[dict(name="pdf-merge", kind="skill", description="Merge pdf"), TOOL]
            )
            result = http.post("/relay/v1/guidance", json=body, headers=auth).json()
            used = dict(
                schema_version=1, recommendation_id=result["recommendation_id"], used=[USED_TOOL]
            )
            told = http.post("/relay/v1/guidance/feedback", json=used, headers=auth).json()
            counts = http.get("/relay/v1/guidance/sync", headers=auth).json()
    finally:
        http_server.shutdown()
    assert told == dict(schema_version=1, queued=True)
    assert counts["feedback_enabled"] is True and counts["feedback_sent"] == 1
    assert index.feedbacks == [(result["recommendation_id"], ["host:mcp_tool:weather"])]
