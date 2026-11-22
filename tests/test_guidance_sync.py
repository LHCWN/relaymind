"""Guidance selector sync through the guidance endpoint (RM47, AC42; spec section 7.7).

A fake router answers select and sync on loopback. Each test reads what the router received.
"""

import asyncio
import itertools
import json
import threading
from dataclasses import replace
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helpers import ScriptedAdapter
from launcher_helpers import write_config
from relaymind.api import guidance as guidance_api
from relaymind.api.guidance import install_guidance_routes
from relaymind.guidance import GuidanceSettings
from relaymind.guidance.sync import GuidanceSync
from relaymind.launcher import LaunchError, build_app, load_config
from relaymind.schema_util import StrictValidator, first_schema_error, load_packaged_schema
from relaymind.selection.asset_http import HttpAssetSelector

AUTH = {"Authorization": "Bearer token"}
SELECT_URL = "http://127.0.0.1:8940/select"
SYNC_URL = "http://127.0.0.1:8940/sync"
SKILL_HASH = "1" * 64
SKILL = dict(
    name="pdf-merge",
    kind="skill",
    description="Merge pdf documents",
    content_hash=SKILL_HASH,
    path="/home/user/.openclaw/skills/pdf-merge/SKILL.md",
    size_bytes=812,
)
TOOL = dict(name="weather", kind="mcp_tool", description="Current weather for a city")
BODY = dict(schema_version=1, task_text="merge these pdf files", assets=[SKILL, TOOL])
SETTINGS = GuidanceSettings(
    enabled=True, primary="http", name="router", version="1", url=SELECT_URL
)
SYNCING = replace(SETTINGS, sync_url=SYNC_URL)
SYNC_REQUEST = StrictValidator(
    load_packaged_schema("relaymind.guidance", "asset_sync_request.v1.json")
)


def reply(value) -> httpx.Response:
    """A streamed JSON answer, as a real router sends it."""
    body = json.dumps(value).encode()
    return httpx.Response(
        200, headers={"Content-Type": "application/json"}, stream=httpx.ByteStream(body)
    )


class Router:
    """A fake asset router: answers select with `outcome`, answers sync, records every request.

    `missing` picks which offered pairs find_missing reports; `inference` is what it says it uses;
    `reject(item)` gives an upload item's rejection reason, or None to accept it; a `sync_status`
    other than 200 fails every sync exchange.
    """

    def __init__(
        self,
        outcome="ranked",
        missing=lambda pairs: [],
        inference="local",
        reject=lambda item: None,
        sync_status=200,
    ):
        self.outcome, self.missing, self.inference = outcome, missing, inference
        self.reject, self.sync_status = reject, sync_status
        self.selects: list[dict] = []
        self.syncs: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/select":
            self.selects.append(body)
            return reply(self.answer(body))
        if request.url.path == "/sync":
            # Every sync request the gateway sends must match the published schema.
            assert first_schema_error(SYNC_REQUEST, body) is None
            self.syncs.append(body)
            if self.sync_status != 200:
                return httpx.Response(self.sync_status)
            find = body["operation"] == "find_missing"
            return reply(
                dict(
                    schema_version=1,
                    operation=body["operation"],
                    inference=self.inference,
                    missing=self.missing(body["assets"]) if find else None,
                    results=None if find else [self.result(item) for item in body["assets"]],
                )
            )
        return httpx.Response(404)

    def result(self, item: dict) -> dict:
        reason = self.reject(item)
        return dict(
            asset_id=item["record"]["asset_id"],
            content_hash=item["record"]["content_hash"],
            status="accepted" if reason is None else "rejected",
            reason=reason,
        )

    def answer(self, request: dict) -> dict:
        ranked = self.outcome == "ranked"
        first = request["catalog"][0]
        candidate = dict(
            asset_id=first["asset_id"],
            kind=first["kind"],
            content_hash=first["content_hash"],
            source_version=first["source_version"],
            score=1.0,
            score_kind="router",
            evidence_refs=[],
        )
        return dict(
            schema_version=2,
            recommendation_id=request["recommendation_id"],
            catalog_revision=request["catalog_revision"],
            host_snapshot_revision=request["host_snapshot_revision"],
            selector=dict(name="router", version="1"),
            mechanism="router.hybrid-bm25",
            outcome=self.outcome,
            decision="confident" if ranked else None,
            candidates=[candidate] if ranked else [],
            index_revision="r1" if ranked else None,
            elapsed_ms=1,
            usage_claims=None,
        )

    def transport(self):
        return httpx.MockTransport(self.handler)


def client(router: Router, settings=SETTINGS, sync=None):
    app = FastAPI()
    app.state.application_token = "token"
    selector = HttpAssetSelector(
        name="router", version="1", url=SELECT_URL, transport_factory=router.transport
    )
    if sync is None and settings.sync_url:
        sync = GuidanceSync(settings, transport_factory=router.transport)
    install_guidance_routes(app, settings, selector=selector, sync=sync)
    return TestClient(app)


def test_the_plugins_skill_hash_is_the_offered_content_hash():
    router = Router()
    answer = client(router).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert answer.status_code == 200
    assert answer.json()["chosen_by"] == "third_party"
    offered = {r["identity"].get("package_id"): r for r in router.selects[0]["catalog"]}
    assert offered["pdf-merge"]["content_hash"] == SKILL_HASH
    # The path and size go to the router only through sync, never in select.
    assert "/home/user" not in json.dumps(router.selects[0])


@pytest.mark.parametrize(
    "asset",
    [
        dict(TOOL, content_hash=SKILL_HASH, path="/tmp/t.json", size_bytes=1),
        {k: v for k, v in SKILL.items() if k != "size_bytes"},
        dict(SKILL, path="relative/SKILL.md"),
        dict(SKILL, path="/home/user/.openclaw/skills/../../.ssh/id_ed25519"),
        dict(SKILL, path="/home/user/.openclaw/skills/pdf-merge/../x/SKILL.md"),
        dict(SKILL, path="/home/user/notes.txt"),
        dict(SKILL, path="/home/user/skills/a\x00/SKILL.md"),
        dict(SKILL, content_hash="A" * 64),
        dict(SKILL, content_hash=SKILL_HASH + "\n"),
        dict(SKILL, size_bytes=1024 * 1024 + 1),
    ],
)
def test_a_skill_file_must_come_whole_and_only_on_a_skill(asset):
    body = dict(BODY, assets=[asset])
    answer = client(Router()).post("/relay/v1/guidance", json=body, headers=AUTH)
    assert answer.status_code == 400
    assert answer.json()["error"]["code"] == "invalid_request"


def test_a_windows_skill_path_is_accepted_and_synced():
    path = "C:\\Users\\u\\.openclaw\\skills\\pdf-merge\\SKILL.md"
    body = dict(BODY, assets=[dict(SKILL, path=path), TOOL])
    router = Router(outcome="not_ready", missing=lambda sent: sent)
    answer = client(router, SYNCING).post("/relay/v1/guidance", json=body, headers=AUTH)
    assert answer.status_code == 200
    assert uploads(router)[0]["assets"][0]["content"]["path"] == path


def pairs(select: dict) -> list[dict]:
    return [dict(asset_id=r["asset_id"], content_hash=r["content_hash"]) for r in select["catalog"]]


def test_not_ready_falls_back_and_then_asks_the_router_what_it_is_missing():
    router = Router(outcome="not_ready")
    answer = client(router, SYNCING).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert answer.status_code == 200
    body = answer.json()
    assert body["chosen_by"] == "word_match" and body["degraded"] is True
    assert dict(stage="third_party", outcome="failed") in [
        {k: s[k] for k in ("stage", "outcome")} for s in body["stages"]
    ]
    assert router.syncs == [
        dict(
            schema_version=1,
            operation="find_missing",
            feeder_id="guidance",
            assets=pairs(router.selects[0]),
        )
    ]


def uploads(router: Router) -> list[dict]:
    return [s for s in router.syncs if s["operation"] == "upload"]


def test_only_the_missing_records_are_uploaded_as_the_exact_select_records():
    # The router has the tool already and misses the skill.
    router = Router(outcome="not_ready", missing=lambda sent: sent[:1])
    client(router, SYNCING).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    skill_record = next(r for r in router.selects[0]["catalog"] if r["kind"] == "skill")
    assert uploads(router) == [
        dict(
            schema_version=1,
            operation="upload",
            feeder_id="guidance",
            assets=[
                dict(
                    record=skill_record,
                    content=dict(kind="file", path=SKILL["path"], size_bytes=812),
                )
            ],
        )
    ]


def test_a_missing_tool_is_uploaded_without_content():
    router = Router(outcome="not_ready", missing=lambda sent: sent)
    client(router, SYNCING).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    sent = {a["record"]["kind"]: a["content"] for a in uploads(router)[0]["assets"]}
    assert sent["mcp_tool"] is None
    assert sent["skill"]["kind"] == "file"


def test_nothing_is_uploaded_when_nothing_is_missing():
    router = Router(outcome="not_ready", missing=lambda sent: [])
    client(router, SYNCING).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert [s["operation"] for s in router.syncs] == ["find_missing"]


def test_a_router_with_remote_inference_gets_no_file_unless_the_operator_allows_it():
    router = Router(outcome="not_ready", missing=lambda sent: sent, inference="remote")
    client(router, SYNCING).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert [a["content"] for a in uploads(router)[0]["assets"]] == [None, None]

    allowed = Router(outcome="not_ready", missing=lambda sent: sent, inference="remote")
    settings = replace(SYNCING, sync_allow_remote=True)
    client(allowed, settings).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert uploads(allowed)[0]["assets"][0]["content"]["path"] == SKILL["path"]


@pytest.mark.parametrize(
    "missing",
    [
        lambda sent: [dict(asset_id="host:skill:other", content_hash="2" * 64)],
        lambda sent: sent[:1] + sent[:1],
        lambda sent: "not a list",
    ],
)
def test_an_invalid_find_missing_answer_uploads_nothing_and_changes_no_answer(missing):
    router = Router(outcome="not_ready", missing=missing)
    http = client(router, SYNCING)
    answer = http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert answer.status_code == 200 and answer.json()["chosen_by"] == "word_match"
    assert uploads(router) == []
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert len(router.syncs) == 1  # An invalid answer is a failure: the next sync waits.


def test_a_failing_sync_endpoint_changes_no_answer():
    router = Router(outcome="not_ready", sync_status=500)
    answer = client(router, SYNCING).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert answer.status_code == 200 and answer.json()["chosen_by"] == "word_match"
    assert len(router.syncs) == 1


def test_without_a_sync_url_not_ready_still_falls_back_and_nothing_is_synced():
    router = Router(outcome="not_ready")
    answer = client(router, SETTINGS).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert answer.json()["chosen_by"] == "word_match"
    assert router.syncs == []


def test_a_ranked_answer_starts_no_sync():
    router = Router()
    client(router, SYNCING).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert router.syncs == []


def test_a_pair_rejected_with_its_file_is_sent_once_more_without_it():
    router = Router(
        outcome="not_ready",
        missing=lambda sent: sent,
        reject=lambda item: "outside_roots" if item["content"] else None,
    )
    client(router, SYNCING).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    first, second = uploads(router)
    assert [a["content"] is not None for a in first["assets"]] == [True, False]
    skill_record = first["assets"][0]["record"]
    assert second["assets"] == [dict(record=skill_record, content=None)]


def test_a_pair_rejected_without_a_file_is_not_sent_again_until_its_hash_changes():
    router = Router(
        outcome="not_ready", missing=lambda sent: sent, reject=lambda item: "invalid_record"
    )
    http, clock = syncing_client(router)
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert len(uploads(router)) == 2  # Both rejected; the skill once more without its file.
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert len(uploads(router)) == 2  # Nothing new: every missing pair was rejected before.
    clock.now += 31  # That sync made no progress, so the next one waits (spec 0.72).
    changed = dict(BODY, assets=[dict(SKILL, content_hash="3" * 64), TOOL])
    http.post("/relay/v1/guidance", json=changed, headers=AUTH)
    latest = uploads(router)[-2]
    assert [a["record"]["content_hash"] for a in latest["assets"]] == ["3" * 64]


def test_an_upload_answer_without_one_result_for_each_pair_ends_the_sync():
    class Partial(Router):
        def result(self, item):
            return super().result(item) if item["record"]["kind"] == "skill" else None

        def handler(self, request):
            answer = super().handler(request)
            if request.url.path == "/sync" and json.loads(request.content)["operation"] == "upload":
                body = json.loads(answer.read())
                body["results"] = [r for r in body["results"] if r is not None]
                return reply(body)
            return answer

    router = Partial(
        outcome="not_ready", missing=lambda sent: sent, reject=lambda item: "too_large"
    )
    answer = client(router, SYNCING).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert answer.status_code == 200
    assert len(uploads(router)) == 1  # No second upload after an answer that misses a pair.


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_failed_file_retry_keeps_the_first_upload_results():
    """RM47, AC42: a failed retry does not forget other confirmed upload results."""
    other = dict(name="notes", kind="skill", description="Write notes")
    body = dict(BODY, assets=[SKILL, TOOL, other])
    router = Router(
        outcome="not_ready",
        missing=lambda sent: sent,
        reject=lambda item: (
            "invalid_record" if item["record"]["description"] != "Write notes" else None
        ),
    )

    def handler(request):
        answer = router.handler(request)
        if len(uploads(router)) == 2 and json.loads(request.content).get("operation") == "upload":
            return httpx.Response(500)
        return answer

    clock = Clock()
    sync = GuidanceSync(
        SYNCING, transport_factory=lambda: httpx.MockTransport(handler), clock=clock
    )
    http = client(router, SYNCING, sync=sync)
    http.post("/relay/v1/guidance", json=body, headers=AUTH)
    failed = status(http)

    clock.now += 31
    http.post("/relay/v1/guidance", json=body, headers=AUTH)
    assert all(a["record"]["kind"] != "mcp_tool" for a in uploads(router)[2]["assets"])
    assert (failed["last_outcome"], failed["accepted"], failed["rejected"]) == ("failed", 1, 1)


def test_a_failed_sync_waits_longer_after_each_failure_and_a_good_one_ends_the_wait():
    router = Router(outcome="not_ready", sync_status=500)
    clock = Clock()
    sync = GuidanceSync(SYNCING, transport_factory=router.transport, clock=clock)
    http = client(router, SYNCING, sync=sync)

    def ask(after=0.0):
        clock.now += after
        http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
        return len(router.syncs)

    assert ask() == 1
    assert ask(29) == 1  # Within 30 s of the first failure.
    assert ask(2) == 2
    assert ask(59) == 2  # The second failure waits 60 s.
    assert ask(2) == 3
    router.sync_status = 200
    assert ask(121) == 4  # It completes: nothing was missing.
    assert ask() == 5  # No wait after a sync that completed.


STATUS = StrictValidator(load_packaged_schema("relaymind.guidance", "guidance_sync_status.v1.json"))


def status(http) -> dict:
    answer = http.get("/relay/v1/guidance/sync", headers=AUTH)
    assert answer.status_code == 200
    body = answer.json()
    assert first_schema_error(STATUS, body) is None
    return body


def test_the_status_route_counts_the_last_sync_and_names_no_asset():
    router = Router(
        outcome="not_ready",
        missing=lambda sent: sent,
        reject=lambda item: "invalid_record" if item["record"]["kind"] == "mcp_tool" else None,
    )
    http = client(router, SYNCING)
    assert status(http)["last_outcome"] == "none"
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    body = status(http)
    assert body == dict(
        schema_version=1,
        enabled=True,
        running=False,
        last_outcome="synced",
        last_inference="local",
        missing=2,
        accepted=1,
        rejected=1,
        files_sent=1,
        consecutive_failures=0,
        retry_after_ms=0,
    )
    text = json.dumps(body)
    assert "pdf-merge" not in text and "weather" not in text and "/home" not in text


def test_the_status_route_reports_a_failure_and_the_wait():
    router = Router(outcome="not_ready", sync_status=500)
    clock = Clock()
    sync = GuidanceSync(SYNCING, transport_factory=router.transport, clock=clock)
    http = client(router, SYNCING, sync=sync)
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    clock.now += 10
    body = status(http)
    assert body["last_outcome"] == "failed" and body["consecutive_failures"] == 1
    assert body["retry_after_ms"] == 20000


def test_the_status_route_without_sync_needs_the_token_and_guidance():
    http = client(Router(), SETTINGS)
    assert http.get("/relay/v1/guidance/sync").status_code == 401
    body = status(http)
    assert body["enabled"] is False and body["missing"] == 0 and body["last_outcome"] == "none"
    off = client(Router(), GuidanceSettings())
    assert off.get("/relay/v1/guidance/sync", headers=AUTH).status_code == 503


class SlowSync(Router):
    """A router whose sync answers take `delay` seconds; select stays fast."""

    def __init__(self, delay, **kw):
        super().__init__(**kw)
        self.delay = delay

    def transport(self):
        async def handler(request):
            answer = self.handler(request)  # Recorded when it arrives, answered after the delay.
            if request.url.path == "/sync":
                await asyncio.sleep(self.delay)
            return answer

        return httpx.MockTransport(handler)


def test_each_sync_exchange_has_its_own_time_not_the_turns():
    # The turn has 0.2 s; a sync exchange that takes 0.3 s still completes within sync_ms.
    router = SlowSync(0.3, outcome="not_ready", missing=lambda sent: sent)
    settings = replace(SYNCING, total_ms=200, sync_ms=1000)
    client(router, settings).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert [s["operation"] for s in router.syncs] == ["find_missing", "upload"]

    late = SlowSync(0.3, outcome="not_ready", missing=lambda sent: sent)
    client(late, replace(settings, sync_ms=100)).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert [s["operation"] for s in late.syncs] == ["find_missing"]


# --- launch settings ------------------------------------------------------------------------


def http_table(base="http://127.0.0.1:8940") -> str:
    return (
        "\n[guidance]\nenabled = true\nprimary = 'http'\nname = 'router'\nversion = '1'\n"
        f"url = '{base}/select'\n"
    )


HTTP_TABLE = http_table()


@pytest.mark.parametrize(
    "table",
    [
        "\n[guidance]\nenabled = true\nsync_url = 'http://127.0.0.1:8940/sync'\n",
        HTTP_TABLE + "sync_url = 'http://example.com/sync'\n",
        HTTP_TABLE + f"sync_url = '{SYNC_URL}'\nsync_allow_remote = 'yes'\n",
        HTTP_TABLE + f"sync_url = '{SYNC_URL}'\nsync_ms = 0\n",
        HTTP_TABLE + f"sync_url = '{SYNC_URL}'\nsync_ms = 600001\n",
    ],
)
def test_bad_sync_settings_stop_startup(tmp_path, table):
    with pytest.raises(LaunchError):
        load_config(write_config(tmp_path, table))


def test_the_launched_gateway_syncs_with_a_real_loopback_router(tmp_path):
    router = Router(outcome="not_ready")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            request = httpx.Request("POST", f"http://127.0.0.1{self.path}", content=body)
            answer = router.handler(request)
            data = answer.read()
            self.send_response(answer.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    table = http_table(base) + f"sync_url = '{base}/sync'\n"
    try:
        config = load_config(write_config(tmp_path, table))
        launched = build_app(config, local_adapter=ScriptedAdapter(turns=[]), external_adapter=None)
        token = launched.app.state.application_token
        with TestClient(launched.app) as http:
            answer = http.post(
                "/relay/v1/guidance", json=BODY, headers={"Authorization": f"Bearer {token}"}
            )
    finally:
        server.shutdown()
    assert answer.json()["chosen_by"] == "word_match"
    assert [s["operation"] for s in router.syncs] == ["find_missing"]


def test_a_sync_is_not_reported_as_synced_while_its_upload_runs():
    """AC42: `synced` means a sync with missing pairs finished (review of PR 291)."""
    router = Router(outcome="not_ready", missing=lambda sent: sent)
    seen = []
    sync = GuidanceSync(SYNCING, transport_factory=lambda: httpx.MockTransport(handler))

    def handler(request):
        if json.loads(request.content)["operation"] == "upload":
            seen.append(sync.status())
        return router.handler(request)

    http = client(router, SYNCING, sync=sync)
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert seen and seen[0]["running"] is True and seen[0]["last_outcome"] == "none"
    assert status(http)["last_outcome"] == "synced" and status(http)["missing"] == 2


def test_a_sync_whose_upload_fails_reports_the_counts_of_its_find_missing():
    """AC42: the counts are those of the last sync whose find_missing was answered."""
    router = Router(outcome="not_ready", missing=lambda sent: sent)

    def handler(request):
        if json.loads(request.content)["operation"] == "upload":
            return httpx.Response(500)
        return router.handler(request)

    sync = GuidanceSync(SYNCING, transport_factory=lambda: httpx.MockTransport(handler))
    http = client(router, SYNCING, sync=sync)
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    body = status(http)
    assert body["last_outcome"] == "failed" and body["missing"] == 2 and body["accepted"] == 0


def test_each_sync_exchange_uses_the_injected_clock(monkeypatch):
    """RM47: the sync passes its own clock to the selector transport."""
    import relaymind.guidance.sync as sync_module

    clocks = []
    real = sync_module.post_json

    async def spy(url, payload, context, **kwargs):
        clocks.append(context.clock)
        return await real(url, payload, context, **kwargs)

    monkeypatch.setattr(sync_module, "post_json", spy)
    router = Router(outcome="not_ready")
    clock = Clock()
    sync = GuidanceSync(SYNCING, transport_factory=router.transport, clock=clock)
    client(router, SYNCING, sync=sync).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert clocks and all(c is clock for c in clocks)


# --- review of PR 291 (seiko-iwasawa) -------------------------------------------------------


def test_the_sync_request_schema_holds_the_exact_asset_record():
    """AC42: `record` is AssetRecordV1, so a third party can validate it; a path is bounded."""
    schema = load_packaged_schema("relaymind.guidance", "asset_sync_request.v1.json")
    upload = schema["$defs"]["upload"]["properties"]
    assert upload["record"] == load_packaged_schema("relaymind.store", "asset_record.v1.json")
    file = next(o for o in upload["content"]["oneOf"] if o.get("type") == "object")
    assert file["properties"]["path"]["maxLength"] == 2048


def test_a_sync_whose_uploads_were_all_accepted_before_waits_like_a_failure():
    """RM47: a pair accepted and missing again was not indexed; it is offered again after a wait."""
    router = Router(outcome="not_ready", missing=lambda sent: sent)
    clock = Clock()
    sync = GuidanceSync(SYNCING, transport_factory=router.transport, clock=clock)
    http = client(router, SYNCING, sync=sync)

    def ask(after=0.0, body=BODY):
        clock.now += after
        http.post("/relay/v1/guidance", json=body, headers=AUTH)
        return len(uploads(router))

    assert ask() == 1  # Both pairs are new: accepted, and the sync completes.
    assert status(http)["consecutive_failures"] == 0
    assert ask() == 2  # Missing again after they were accepted: uploaded once more...
    body = status(http)
    assert body["last_outcome"] == "synced" and body["accepted"] == 2
    assert body["consecutive_failures"] == 1 and body["retry_after_ms"] == 30000
    assert ask(29) == 2  # ...and then only after the wait.
    assert ask(2) == 3
    assert status(http)["retry_after_ms"] == 60000
    # A new pair is progress: that sync completes and ends the wait.
    changed = dict(BODY, assets=[dict(SKILL, content_hash="3" * 64), TOOL])
    assert ask(61, changed) == 4
    assert status(http)["consecutive_failures"] == 0 and status(http)["retry_after_ms"] == 0


def test_not_ready_without_the_fallback_is_a_degraded_no_match_and_still_syncs():
    router = Router(outcome="not_ready")
    settings = replace(SYNCING, fallback=False)
    answer = client(router, settings).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    body = answer.json()
    assert answer.status_code == 200
    assert body["decision"] == "no_match" and body["degraded"] is True and body["picks"] == []
    assert [s["operation"] for s in router.syncs] == ["find_missing"]


def test_a_sync_wanted_while_one_runs_is_skipped():
    router = SlowSync(0.2, outcome="not_ready", missing=lambda sent: [])
    sync = GuidanceSync(SYNCING, transport_factory=router.transport)
    catalog = [dict(asset_id="host:skill:a", content_hash=SKILL_HASH)]

    async def both():
        first = asyncio.create_task(sync.run(catalog, {}))
        await asyncio.sleep(0.05)
        assert sync.status()["running"] is True
        await sync.run(catalog, {})  # Returns at once: one sync at a time.
        await first

    asyncio.run(both())
    assert len(router.syncs) == 1
    assert sync.status()["last_outcome"] == "nothing_missing"


def test_the_rejected_pairs_are_capped_with_the_oldest_dropped_first(monkeypatch):
    import relaymind.guidance.sync as sync_module

    assert sync_module.MAX_REJECTED == 10_000
    monkeypatch.setattr(sync_module, "MAX_REJECTED", 1)
    router = Router(
        outcome="not_ready",
        missing=lambda sent: [p for p in sent if p["asset_id"].endswith("pdf-merge")],
        reject=lambda item: "invalid_record",
    )
    http, clock = syncing_client(router)

    def ask(content_hash):
        clock.now += 1000  # Past any wait: this test is about the cap, not the wait.
        body = dict(BODY, assets=[dict(SKILL, content_hash=content_hash)])
        http.post("/relay/v1/guidance", json=body, headers=AUTH)
        return len(uploads(router))

    assert ask("4" * 64) == 2  # Rejected with its file, then without: remembered.
    assert ask("4" * 64) == 2  # Not sent again.
    assert ask("5" * 64) == 4  # A new pair, remembered instead of the first.
    assert ask("4" * 64) == 6  # The first was dropped, so it is sent again.


@pytest.mark.parametrize(
    "change",
    [
        lambda body: dict(body, operation="find_missing"),
        lambda body: dict(body, inference="cloud"),
        lambda body: {k: v for k, v in body.items() if k != "inference"},
    ],
)
def test_an_invalid_upload_answer_fails_the_sync_and_changes_no_answer(change):
    router = Router(outcome="not_ready", missing=lambda sent: sent)

    def handler(request):
        answer = router.handler(request)
        if json.loads(request.content)["operation"] == "upload":
            return reply(change(json.loads(answer.read())))
        return answer

    sync = GuidanceSync(SYNCING, transport_factory=lambda: httpx.MockTransport(handler))
    http = client(router, SYNCING, sync=sync)
    answer = http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert answer.status_code == 200 and answer.json()["chosen_by"] == "word_match"
    assert status(http)["last_outcome"] == "failed" and status(http)["accepted"] == 0


def test_an_unknown_inference_in_find_missing_uploads_nothing():
    router = Router(outcome="not_ready", missing=lambda sent: sent, inference="cloud")
    http = client(router, SYNCING)
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert uploads(router) == []
    assert status(http)["last_outcome"] == "failed"


# --- the reference selector: scripts/guidance_selector_server.py --sync-root ----------------


def skill_file(folder):
    """The pdf-merge skill as a real SKILL.md, with the file fields the plugin sends."""
    from router_interop import skill_file as write_skill

    return write_skill(folder, "pdf-merge", "Merge pdf documents", "Body")


def reference_server():
    from helpers import load_script_module

    return load_script_module("guidance_selector_server")


def upload_item(record_hash, path, size, asset_id="host:skill:pdf-merge"):
    from test_asset_catalog import record

    content = None if path is None else dict(kind="file", path=str(path), size_bytes=size)
    return dict(
        record=record(asset_id=asset_id, kind="skill", content_hash=record_hash), content=content
    )


def sync_request(operation, assets):
    return dict(schema_version=1, operation=operation, feeder_id="guidance", assets=assets)


def test_the_reference_selector_reads_only_real_paths_under_its_roots(tmp_path):
    """RM47 spec 0.72: the selector checks size, SHA-256 and roots against the real path."""
    import os

    server = reference_server()
    root, outside = tmp_path / "skills", tmp_path / "elsewhere"
    good = skill_file(root)
    away = skill_file(outside)
    linked = root / "linked" / "SKILL.md"
    linked.parent.mkdir()
    os.symlink(away["path"], linked)
    index = server.SyncIndex([root])
    items = [
        upload_item(good["content_hash"], good["path"], good["size_bytes"]),
        upload_item(away["content_hash"], away["path"], away["size_bytes"], "a:outside"),
        upload_item(away["content_hash"], linked, away["size_bytes"], "a:link"),
        upload_item("0" * 64, good["path"], good["size_bytes"], "a:hash"),
        upload_item(good["content_hash"], good["path"], good["size_bytes"] + 1, "a:size"),
        upload_item(good["content_hash"], root / "gone" / "SKILL.md", 1, "a:gone"),
        upload_item("6" * 64, None, None, "a:record"),
    ]
    answer = index.handle(sync_request("upload", items))
    assert answer["missing"] is None and answer["inference"] == "local"
    assert [(r["asset_id"], r["status"], r["reason"]) for r in answer["results"]] == [
        ("host:skill:pdf-merge", "accepted", None),
        ("a:outside", "rejected", "outside_roots"),
        ("a:link", "rejected", "outside_roots"),
        ("a:hash", "rejected", "hash_mismatch"),
        ("a:size", "rejected", "size_mismatch"),
        ("a:gone", "rejected", "unreadable"),
        ("a:record", "accepted", None),
    ]
    pairs = [
        dict(asset_id=i["record"]["asset_id"], content_hash=i["record"]["content_hash"])
        for i in items
    ]
    found = index.handle(sync_request("find_missing", pairs))
    assert found["results"] is None
    missing = [p["asset_id"] for p in found["missing"]]
    assert missing == ["a:outside", "a:link", "a:hash", "a:size", "a:gone"]


def test_the_reference_selector_rejects_a_path_with_a_nul_and_takes_the_rest(tmp_path):
    """RM47 spec 0.72: a rejected pair does not stop the others, even for a path it cannot open."""
    server = reference_server()
    root = tmp_path / "skills"
    good = skill_file(root)
    index = server.SyncIndex([root])
    items = [
        upload_item(good["content_hash"], f"{root}/a\0b/SKILL.md", 1, "a:nul"),
        upload_item(good["content_hash"], good["path"], good["size_bytes"]),
    ]
    answer = index.handle(sync_request("upload", items))
    assert [(r["asset_id"], r["status"], r["reason"]) for r in answer["results"]] == [
        ("a:nul", "rejected", "unreadable"),
        ("host:skill:pdf-merge", "accepted", None),
    ]


def test_the_reference_selector_rejects_a_directory_and_takes_the_rest(tmp_path):
    """RM47, AC42: a non-file path is unreadable without failing the entire upload."""
    good = skill_file(tmp_path)
    directory = tmp_path / "directory" / "SKILL.md"
    directory.mkdir(parents=True)
    items = [
        upload_item(good["content_hash"], directory, 0, "a:directory"),
        upload_item(good["content_hash"], good["path"], good["size_bytes"]),
    ]
    answer = reference_server().SyncIndex([tmp_path]).handle(sync_request("upload", items))
    assert [(r["asset_id"], r["status"], r["reason"]) for r in answer["results"]] == [
        ("a:directory", "rejected", "unreadable"),
        ("host:skill:pdf-merge", "accepted", None),
    ]


def test_the_reference_selector_requires_a_verifiable_file_identity(tmp_path, monkeypatch):
    """RM47, AC42: matching unavailable file IDs do not prove the file is under an allowed root."""
    import os
    from pathlib import Path

    good = skill_file(tmp_path)
    path = Path(good["path"]).resolve()
    stat_path = Path.stat
    stat_fd = os.fstat

    def without_identity(info):
        values = list(info)
        values[1] = 0  # st_ino
        return os.stat_result(values)

    def path_stat(candidate, *args, **kwargs):
        info = stat_path(candidate, *args, **kwargs)
        return without_identity(info) if candidate == path else info

    monkeypatch.setattr(Path, "stat", path_stat)
    monkeypatch.setattr(os, "fstat", lambda fd: without_identity(stat_fd(fd)))
    item = upload_item(good["content_hash"], path, good["size_bytes"])
    answer = reference_server().SyncIndex([tmp_path]).handle(sync_request("upload", [item]))
    assert [(result["status"], result["reason"]) for result in answer["results"]] == [
        ("rejected", "unreadable")
    ]


def test_the_launched_gateway_syncs_the_reference_selector_until_it_ranks(tmp_path):
    """AC42: not_ready -> find_missing -> upload with the file -> the next select is ranked."""
    server = reference_server()
    asset = skill_file(tmp_path / "skills")
    body = dict(BODY, assets=[asset, TOOL])
    index = server.SyncIndex([tmp_path / "skills"])
    settings = GuidanceSettings(enabled=True)
    seen = []

    def answer(value):
        seen.append(value["catalog"])
        return asyncio.run(server.select(value, settings, name="router", version="1", index=index))

    def sync(value):
        seen.append(value["operation"])
        return index.handle(value)

    http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(answer, sync))
    threading.Thread(target=http_server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http_server.server_port}"
    table = http_table(base) + f"sync_url = '{base}/sync'\n"
    try:
        config = load_config(write_config(tmp_path, table))
        launched = build_app(config, local_adapter=ScriptedAdapter(turns=[]), external_adapter=None)
        auth = {"Authorization": f"Bearer {launched.app.state.application_token}"}
        with TestClient(launched.app) as http:
            first = http.post("/relay/v1/guidance", json=body, headers=auth).json()
            synced = http.get("/relay/v1/guidance/sync", headers=auth).json()
            second = http.post("/relay/v1/guidance", json=body, headers=auth).json()
    finally:
        http_server.shutdown()
    assert first["chosen_by"] == "word_match" and first["degraded"] is True
    assert [s for s in seen if isinstance(s, str)] == ["find_missing", "upload"]
    assert synced["last_outcome"] == "synced" and synced["files_sent"] == 1
    assert synced["accepted"] == 2 and synced["rejected"] == 0
    assert second["chosen_by"] == "third_party" and second["degraded"] is False
    assert [p["name"] for p in second["picks"]] == ["pdf-merge"]


# --- identity, deadline and freshness (router integration review) --------------------------


def test_the_real_selection_request_carries_a_fractional_deadline():
    """SelectionRequestV2.deadline_ms is a positive number: built and sent with its fraction."""
    from relaymind.guidance import parse_guidance_request, run_guidance

    sent: list[bytes] = []
    router = Router()

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return router.handler(request)

    selector = HttpAssetSelector(
        name="router",
        version="1",
        url=SELECT_URL,
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    settings = replace(SETTINGS, total_ms=7777)
    clock = itertools.count(1000.0, 0.0001234).__next__
    answer = asyncio.run(
        run_guidance(parse_guidance_request(BODY), settings, selector=selector, clock=clock)
    )
    assert answer["chosen_by"] == "third_party"
    request = json.loads(sent[0])
    deadline = request["deadline_ms"]
    assert deadline != int(deadline) and 0 < deadline < 7777
    schema = StrictValidator(
        load_packaged_schema("relaymind.selection", "selection_request.v2.json")
    )
    assert first_schema_error(schema, request) is None
    assert f'"deadline_ms":{deadline!r}'.encode() in sent[0]  # Not rounded on the wire.


@pytest.mark.parametrize("tick", [0.0, 1 / 8192], ids=["whole-ms", "fractional-ms"])
def test_the_guidance_route_sends_the_deadline_from_the_clock(monkeypatch, tick):
    """RM47, AC42: whole and fractional deadlines both survive the route and transport."""
    clock = itertools.count(1000.0, tick).__next__
    monkeypatch.setattr(
        guidance_api, "run_guidance", partial(guidance_api.run_guidance, clock=clock)
    )
    router = Router()
    answer = client(router).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert answer.json()["chosen_by"] == "third_party"
    deadline = router.selects[0]["deadline_ms"]
    assert 0 < deadline <= SETTINGS.total_ms
    assert (deadline != int(deadline)) == bool(tick)


def test_an_unchanged_asset_keeps_its_identity_whatever_the_request():
    """Session, task and recommendation IDs and the catalog revision never enter source identity."""
    router = Router()
    http = client(router)
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    other = dict(schema_version=1, task_text="join two pdfs", recent_text="earlier", assets=[SKILL])
    http.post("/relay/v1/guidance", json=other, headers=AUTH)
    first, second = router.selects
    skill = [next(r for r in s["catalog"] if r["kind"] == "skill") for s in (first, second)]
    assert skill[0] == skill[1]  # The whole record: asset_id, content_hash, description, identity.
    assert skill[0]["content_hash"] == SKILL_HASH
    assert first["recommendation_id"] != second["recommendation_id"]
    assert first["task_id"] != second["task_id"]
    assert first["catalog_revision"] != second["catalog_revision"]  # The offer changed.


def test_identical_requests_get_the_selectors_latest_answer():
    """No ranking cache: an identical request asks the selector again and admits its new answer."""

    class Changing(Router):
        def answer(self, request):
            result = super().answer(request)
            pick = request["catalog"][(len(self.selects) - 1) % len(request["catalog"])]
            result["candidates"][0].update(
                asset_id=pick["asset_id"],
                kind=pick["kind"],
                content_hash=pick["content_hash"],
                source_version=pick["source_version"],
            )
            result["index_revision"] = f"r{len(self.selects)}"
            return result

    router = Changing()
    http = client(router)
    answers = [http.post("/relay/v1/guidance", json=BODY, headers=AUTH).json() for _ in range(2)]
    assert len(router.selects) == 2
    assert [a["picks"][0]["name"] for a in answers] == ["pdf-merge", "weather"]


def test_a_shortened_description_keeps_the_file_hash_and_is_offered_as_it_is_now():
    """Spec 0.72 7.7: the pair covers the file, not the description; a selector ranks with the
    description of the current select. Same SKILL.md, two projections, one source identity."""
    router = Router()
    http = client(router, SYNCING)
    for description in ("Merge pdf documents into one file", "Merge pdf"):
        body = dict(BODY, assets=[dict(SKILL, description=description)])
        assert http.post("/relay/v1/guidance", json=body, headers=AUTH).status_code == 200
    records = [s["catalog"][0] for s in router.selects]
    assert [r["description"] for r in records] == ["Merge pdf documents into one file", "Merge pdf"]
    assert records[0]["content_hash"] == records[1]["content_hash"] == SKILL_HASH
    assert router.syncs == []  # Ranked both times: a description change starts no sync.


def test_the_reference_selector_checks_the_file_it_opened_not_only_the_path_it_resolved(
    tmp_path, monkeypatch
):
    """RM47 spec 0.72: the real path of the file the selector opens must be under its roots.

    A folder on the path is swapped for a link outside the roots after the roots were checked
    and before the file is opened. The selector must notice and refuse it.
    """
    import os

    server = reference_server()
    root, outside = tmp_path / "skills", tmp_path / "elsewhere"
    good = skill_file(root)
    away = skill_file(outside)
    assert good["content_hash"] == away["content_hash"]  # Same bytes: only the location differs.
    real_open = os.open

    def swapping_open(path, *args, **kwargs):
        folder = root / "pdf-merge"
        if folder.is_dir() and not folder.is_symlink():
            folder.rename(root / "pdf-merge-moved")
            os.symlink(outside / "pdf-merge", folder)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(server.os, "open", swapping_open)
    item = upload_item(good["content_hash"], good["path"], good["size_bytes"])
    answer = server.SyncIndex([root]).handle(sync_request("upload", [item]))
    assert [(r["status"], r["reason"]) for r in answer["results"]] == [
        ("rejected", "outside_roots")
    ]


# --- progress and status (spec 0.72, open items after review) ------------------------------


def syncing_client(router: Router):
    clock = Clock()
    sync = GuidanceSync(SYNCING, transport_factory=router.transport, clock=clock)
    return client(router, SYNCING, sync=sync), clock


def test_a_changed_record_for_an_accepted_pair_is_progress():
    """A new description for the same file is a new record: re-sending it is not a stuck pair."""
    router = Router(outcome="not_ready", missing=lambda sent: sent)
    http, _ = syncing_client(router)
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    renamed = dict(BODY, assets=[dict(SKILL, description="Merge pdf"), TOOL])
    http.post("/relay/v1/guidance", json=renamed, headers=AUTH)
    assert len(uploads(router)) == 2
    assert status(http)["consecutive_failures"] == 0  # The skill's record changed: progress.
    http.post("/relay/v1/guidance", json=renamed, headers=AUTH)
    assert len(uploads(router)) == 3
    assert status(http)["consecutive_failures"] == 1  # The same records again: no progress.


def test_a_sync_whose_missing_pairs_were_all_rejected_before_waits():
    router = Router(
        outcome="not_ready", missing=lambda sent: sent, reject=lambda item: "invalid_record"
    )
    http, clock = syncing_client(router)
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert len(uploads(router)) == 2  # Both rejected; the skill once more without its file.
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    body = status(http)
    assert len(uploads(router)) == 2 and len(router.syncs) == 4  # find_missing, no upload.
    assert body["last_outcome"] == "synced" and body["rejected"] == 2
    assert body["consecutive_failures"] == 1 and body["retry_after_ms"] == 30000
    clock.now += 29
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert len(router.syncs) == 4  # Within the wait: not even a find_missing.


def test_a_failed_find_missing_is_reported_with_zero_counts():
    router = Router(outcome="not_ready", missing=lambda sent: sent)
    http, _ = syncing_client(router)
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert status(http)["missing"] == 2
    router.sync_status = 500
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    body = status(http)
    assert body["last_outcome"] == "failed" and body["last_inference"] is None
    assert [body[k] for k in ("missing", "accepted", "rejected", "files_sent")] == [0, 0, 0, 0]
