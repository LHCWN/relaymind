"""Guidance selector sync through the guidance endpoint (RM47, AC42; spec section 7.7).

A fake router answers select and sync on loopback. Each test reads what the router received.
"""

import asyncio
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helpers import ScriptedAdapter
from launcher_helpers import write_config
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
    http = client(router, SYNCING)
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert len(uploads(router)) == 2  # Both rejected; the skill once more without its file.
    http.post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert len(uploads(router)) == 2  # Nothing new: every missing pair was rejected before.
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
