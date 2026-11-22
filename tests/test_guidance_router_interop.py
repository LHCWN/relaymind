"""The real guidance gateway against the real asset-router over HTTP (RM47, AC42; spec 0.72 7.7).

Marked `interop` and skipped unless RELAYMIND_ASSET_ROUTER_DIR names an asset-router checkout with
its dependencies installed. scripts/asset_router_interop.py prepares one at an exact commit and runs
this module:

  python scripts/asset_router_interop.py --rev <router commit>

or by hand:

  RELAYMIND_ASSET_ROUTER_DIR=<checkout> RELAYMIND_ASSET_ROUTER_REV=<commit> pytest -m interop \\
      tests/test_guidance_router_interop.py

Both sides run production code: the gateway's guidance route, selection request construction and
serialization, response validation and selector sync; the router's own server entry point, schema
validation, /sync and /select handling and SQLite store. Only inference is a double (a deterministic
embeddings endpoint). A recording proxy shows each exchange, and every test that expects a ranking
checks that the gateway's picks are the router's candidates, so a word-match fallback cannot pass.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from relaymind.api import guidance as guidance_api
from relaymind.api.guidance import install_guidance_routes
from relaymind.guidance import GuidanceSettings
from relaymind.guidance.sync import GuidanceSync
from relaymind.schema_util import StrictValidator, first_schema_error, load_packaged_schema
from relaymind.selection.asset_http import HttpAssetSelector
from router_interop import (
    ROUTER_NAME,
    ROUTER_REV_ENV,
    ROUTER_VERSION,
    EmbeddingDouble,
    RecordingProxy,
    RouterProcess,
    poll,
    require_checkout,
    router_revision,
    skill_file,
)
from test_guidance_sync import sync_request

pytestmark = pytest.mark.interop

AUTH = {"Authorization": "Bearer token"}
SYNC_RESPONSE = StrictValidator(
    load_packaged_schema("relaymind.guidance", "asset_sync_response.v1.json")
)
SELECT_RESULT = StrictValidator(
    load_packaged_schema("relaymind.selection", "selection_result.v2.json")
)
TOOL = dict(name="weather", kind="mcp_tool", description="Current weather for a city")
TASK = "merge these pdf files into one document"


@pytest.fixture(scope="module")
def checkout() -> Path:
    return require_checkout()


@pytest.fixture(scope="module")
def revision(checkout: Path) -> str:
    """The exact router commit, printed so a run records what it was checked against."""
    head = router_revision(checkout)
    expected = os.environ.get(ROUTER_REV_ENV)
    if expected:
        assert head == expected, f"router checkout is at {head}, expected {expected}"
    print(f"\nasset-router revision: {head}")
    return head


class Clock:
    """Real time plus an offset, so a test can pass the gateway's sync wait at once."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


def name_of(record: dict) -> str:
    """The host name an offered record stands for; the gateway's asset ID format is its own."""
    identity = record["identity"]
    return identity.get("package_id") or identity["tool_name"]


def pairs(records: list[dict]) -> list[dict]:
    return [dict(asset_id=r["asset_id"], content_hash=r["content_hash"]) for r in records]


@dataclass
class Stack:
    router: RouterProcess
    embedder: EmbeddingDouble
    proxy: RecordingProxy
    http: TestClient
    sync: GuidanceSync
    clock: Clock
    client: httpx.Client
    root: Path

    def ask(self, assets: list[dict], task: str = TASK) -> dict:
        body = dict(schema_version=1, task_text=task, assets=assets)
        answer = self.http.post("/relay/v1/guidance", json=body, headers=AUTH)
        assert answer.status_code == 200, answer.text
        return answer.json()

    def selects(self):
        return self.proxy.of("/select")

    def uploads(self):
        return self.proxy.of("/sync", "upload")

    def finds(self):
        return self.proxy.of("/sync", "find_missing")

    def status(self) -> dict:
        return self.http.get("/relay/v1/guidance/sync", headers=AUTH).json()

    def offered(self) -> list[dict]:
        """The catalog the gateway itself built for its last select."""
        return self.selects()[-1].request["catalog"]

    def id_of(self, name: str) -> str:
        return next(r["asset_id"] for r in self.offered() if name_of(r) == name)

    def diagnostics(self) -> str:
        last = self.selects()[-1] if self.selects() else None
        finds = self.finds()
        return (
            f"last select {last and (last.status, last.response)}; "
            f"last find_missing {finds and finds[-1].response}; sync status {self.status()}"
        )

    def pass_sync_wait(self) -> None:
        """Move the gateway's clock past its current sync wait, as long as that wait is."""
        wait_ms = self.status()["retry_after_ms"]
        if wait_ms:
            self.clock.offset += wait_ms / 1000 + 0.001

    def until_router_answers(
        self, assets: list[dict], task: str = TASK, timeout: float = 30, *, move_clock=True
    ):
        """Ask until the router answers with something other than not_ready.

        With `move_clock` false the clock never moves, and no sync may start a wait."""

        def answered():
            answer = self.ask(assets, task)
            if not move_clock:  # After every ask, the answered one too.
                body = self.status()
                assert body["retry_after_ms"] == 0 and body["consecutive_failures"] == 0, (
                    f"a sync started the wait: {self.diagnostics()}"
                )
            last = self.selects()[-1]
            if last.status == 200 and last.response.get("outcome") not in (None, "not_ready"):
                return answer, last.response
            if move_clock:
                self.pass_sync_wait()
            return None

        return poll(answered, timeout, lambda: f"the router never answered: {self.diagnostics()}")

    def until_ranked(
        self, assets: list[dict], task: str = TASK, timeout: float = 30, *, move_clock=True
    ) -> dict:
        """Ask until the router itself ranks; the gateway's picks must be its candidates."""
        answer, _ = self.until_router_answers(assets, task, timeout, move_clock=move_clock)
        assert answer["chosen_by"] == "third_party", self.diagnostics()
        assert_from_router(self, answer)
        return answer

    def require_uploads(self, count: int = 1):
        """At least `count` uploads, or a failure that says where the sync stopped."""
        uploads = self.uploads()
        if len(uploads) < count:
            pytest.fail(f"{len(uploads)} uploads, expected {count}; {self.diagnostics()}")
        return uploads

    def router_missing(self, records: list[dict]) -> list[str]:
        """The router's own find_missing for these records, asked directly."""
        answer = self.client.post(
            self.router.url + "/sync", json=sync_request("find_missing", pairs(records))
        )
        return [p["asset_id"] for p in answer.json()["missing"]]

    def wait_missing(self, records: list[dict], expected: list[str], timeout: float = 20) -> None:
        """Wait until the router itself reports exactly these asset IDs missing."""
        poll(
            lambda: self.router_missing(records) == expected,
            timeout,
            f"router never reported {expected} missing",
        )


def assert_from_router(stack: Stack, answer: dict) -> None:
    """The answer's picks are the candidates of the router's last ranked result, in order."""
    last = stack.selects()[-1]
    assert last.status == 200 and last.response["outcome"] == "ranked", last.response
    assert first_schema_error(SELECT_RESULT, last.response) is None
    offered = {r["asset_id"]: r for r in last.request["catalog"]}
    names = [name_of(offered[c["asset_id"]]) for c in last.response["candidates"]]
    assert [p["name"] for p in answer["picks"]] == names[: len(answer["picks"])]
    assert answer["mechanism"] == last.response["mechanism"]
    assert answer["degraded"] is False


def assert_valid_sync(exchange) -> None:
    assert exchange.status == 200, exchange.response
    error = first_schema_error(SYNC_RESPONSE, exchange.response)
    assert error is None, (
        f"{exchange.request['operation']} answer breaks AssetSyncResponseV1: {error}"
    )


@pytest.fixture
def stack(checkout, revision, tmp_path) -> Iterator[Stack]:
    root = tmp_path / "skills"
    root.mkdir()
    embedder = EmbeddingDouble().start()
    router = RouterProcess(checkout, tmp_path / "router", embedder.url, [root])
    router.workdir.mkdir()
    router.start()
    proxy = RecordingProxy(target=router.url).start()
    clock = Clock()
    settings = GuidanceSettings(
        enabled=True,
        primary="http",
        name=ROUTER_NAME,
        version=ROUTER_VERSION,
        url=proxy.url + "/select",
        sync_url=proxy.url + "/sync",
        total_ms=8000,
        sync_ms=10000,
    )
    sync = GuidanceSync(settings, clock=clock)
    app = FastAPI()
    app.state.application_token = "token"
    selector = HttpAssetSelector(name=ROUTER_NAME, version=ROUTER_VERSION, url=settings.url)
    install_guidance_routes(app, settings, selector=selector, sync=sync)
    try:
        with TestClient(app) as http, httpx.Client(timeout=10) as client:
            yield Stack(router, embedder, proxy, http, sync, clock, client, root)
    finally:
        proxy.stop()
        router.stop()
        embedder.stop()


def pdf_skill(stack: Stack) -> dict:
    return skill_file(
        stack.root, "pdf-merge", "Merge pdf documents", "Join several PDF files into one document."
    )


def notes_skill(stack: Stack) -> dict:
    return skill_file(
        stack.root, "meeting-notes", "Write meeting notes", "Summarize a meeting into notes."
    )


# --- contract probes: each side's own validators on the other side's answers ---------------


def test_the_gateway_sync_and_the_router_agree_on_the_sync_wire_format(stack):
    """find_missing and upload answers satisfy AssetSyncResponseV1, as the gateway validates it."""
    assets = [pdf_skill(stack), TOOL]
    stack.ask(assets)
    if not stack.finds():
        # The select did not reach not_ready, so sync the catalog the gateway built directly.
        catalog = stack.offered()
        by_name = {a["name"]: a for a in assets}
        files = {
            r["asset_id"]: dict(path=a["path"], size_bytes=a["size_bytes"])
            for r in catalog
            if "path" in (a := by_name[name_of(r)])
        }
        asyncio.run(stack.sync.run(catalog, files))
    assert stack.finds(), "the gateway sent no find_missing"
    assert_valid_sync(stack.finds()[0])
    assert_valid_sync(stack.require_uploads()[0])
    assert stack.status()["last_outcome"] == "synced"


def test_a_fractional_deadline_from_the_real_request_is_accepted(stack, monkeypatch):
    """SelectionRequestV2.deadline_ms is a positive number; the gateway sends fractions."""
    clock = itertools.count(1000.0, 1 / 8192).__next__
    monkeypatch.setattr(
        guidance_api, "run_guidance", partial(guidance_api.run_guidance, clock=clock)
    )
    stack.ask([pdf_skill(stack), TOOL])
    first = stack.selects()[0]
    deadline = first.request["deadline_ms"]
    assert deadline != int(deadline), deadline
    assert first.status == 200, f"router refused deadline_ms={deadline}: {first.response}"
    assert first_schema_error(SELECT_RESULT, first.response) is None


# --- the lifecycle ---------------------------------------------------------------------------


def test_a_cold_router_is_synced_until_its_own_ranking_answers(stack):
    assets = [pdf_skill(stack), notes_skill(stack), TOOL]
    first = stack.ask(assets)
    assert first["chosen_by"] == "word_match" and first["degraded"] is True
    select = stack.selects()[0]
    assert select.status == 200, f"router refused the select: {select.response}"
    assert select.response["outcome"] == "not_ready", select.response
    find = stack.finds()[0]
    assert_valid_sync(find)
    assert find.request["assets"] == pairs(select.request["catalog"])
    upload = stack.require_uploads()[0]
    assert_valid_sync(upload)
    sent = {name_of(i["record"]): i["content"] for i in upload.request["assets"]}
    assert sent["pdf-merge"]["path"] == assets[0]["path"]
    assert sent["weather"] is None

    answer = stack.until_ranked(assets)
    assert answer["picks"][0]["name"] == "pdf-merge"


def test_a_mixed_upload_is_answered_pair_by_pair_and_a_refused_file_is_retried_without_it(
    stack, tmp_path
):
    refused = skill_file(tmp_path / "elsewhere", "outside", "Merge pdf pages", "Outside the roots.")
    assets = [pdf_skill(stack), refused, TOOL]
    stack.ask(assets)
    first, *rest = stack.require_uploads()
    assert_valid_sync(first)
    records = {r["asset_id"]: r for r in (i["record"] for i in first.request["assets"])}
    results = {
        name_of(records[r["asset_id"]]): (r["status"], r["reason"])
        for r in first.response["results"]
    }
    assert results == {
        "pdf-merge": ("accepted", None),
        "outside": ("rejected", "outside_roots"),
        "weather": ("accepted", None),
    }
    # The gateway sends the refused skill once more, without its file.
    assert rest, "no second upload without the file"
    assert rest[0].request["assets"] == [dict(record=records[stack.id_of("outside")], content=None)]
    assert_valid_sync(rest[0])
    assert rest[0].response["results"][0]["status"] == "accepted"
    stack.until_ranked(assets)


def test_an_indexing_failure_after_acceptance_is_reconciled_again(stack):
    """Accepted is not indexed: a failed index is missing again, and the gateway retries it."""
    stack.embedder.fail_marker = "flakymarker"
    broken = skill_file(
        stack.root, "pdf-merge", "Merge pdf documents", "flakymarker join pdf files"
    )
    assets = [broken]
    stack.ask(assets)
    upload = stack.require_uploads()[0]
    assert_valid_sync(upload)
    assert upload.response["results"][0]["status"] == "accepted"
    catalog, pdf = stack.offered(), stack.id_of("pdf-merge")
    stack.wait_missing(catalog, [pdf])
    stack.ask(assets)  # not_ready again: missing again, uploaded again, and the gateway waits.
    assert len(stack.require_uploads(2)) == 2
    assert stack.status()["consecutive_failures"] == 1
    stack.ask(assets)  # Within the wait: no third upload.
    assert len(stack.uploads()) == 2
    stack.wait_missing(catalog, [pdf])  # The retry failed too.
    stack.embedder.fail_marker = None
    stack.until_ranked(assets, task="join pdf files")  # Passes the wait, then syncs again.
    assert len(stack.uploads()) == 3


def test_an_unchanged_source_is_reused_across_different_offers(stack):
    pdf, notes = pdf_skill(stack), notes_skill(stack)
    stack.until_ranked([pdf, notes])
    uploads = len(stack.uploads())
    stack.until_ranked([pdf])  # A subset: no sync at all.
    assert len(stack.uploads()) == uploads
    extra = skill_file(stack.root, "pdf-split", "Split pdf documents", "Cut a PDF into pages.")
    stack.until_ranked([pdf, extra])
    new = [name_of(i["record"]) for u in stack.uploads()[uploads:] for i in u.request["assets"]]
    assert new == ["pdf-split"]  # The unchanged pdf-merge is never sent again.


def test_identical_requests_follow_the_router_after_a_feature_recipe_is_published(stack):
    """content_hash names the source; the router's recipes decide what it derives and reuses.

    With description embeddings only, the router's valid answer to this task is no_match. After a
    full-body recipe is published on the same store, the same request gets its ranked answer. The
    gateway asks the selector on every request, so each answer is the router's answer at that time.
    """
    stack.router.restart(embedding_source="description")
    assets = [pdf_skill(stack), notes_skill(stack)]
    before, result = stack.until_router_answers(assets)
    assert result["outcome"] == "no_match", result
    assert before["decision"] == "no_match" and before["degraded"] is False
    assert {"stage": "third_party", "outcome": "ok"} in [
        {k: s[k] for k in ("stage", "outcome")} for s in before["stages"]
    ]
    revision_before, offered = result["index_revision"], stack.offered()
    uploads = len(stack.uploads())
    # Same sources, same store: only the recipe is new. A router may derive the new feature from
    # inputs it stored, or answer not_ready until the pairs are synced again.
    stack.router.restart(embedding_source="full")
    after = stack.until_ranked(assets)
    assert after["picks"][0]["name"] == "pdf-merge"
    assert stack.selects()[-1].response["index_revision"] != revision_before
    resent = [
        p
        for u in stack.uploads()[uploads:]
        for p in pairs([i["record"] for i in u.request["assets"]])
    ]
    # Anything sent again is the same source identity: the recipe never changes content_hash.
    assert all(p in pairs(offered) for p in resent)


def test_a_restarted_router_reuses_its_persisted_inputs(stack):
    assets = [pdf_skill(stack), notes_skill(stack), TOOL]
    stack.until_ranked(assets)
    syncs = len(stack.proxy.of("/sync"))
    stack.router.restart()
    calls = stack.embedder.calls
    answer = stack.ask(assets)
    assert answer["chosen_by"] == "third_party", stack.diagnostics()
    assert_from_router(stack, answer)
    assert len(stack.proxy.of("/sync")) == syncs  # Nothing re-sent after the restart.
    assert stack.embedder.calls - calls <= 1  # Only the query is embedded, never a body again.


def test_a_new_description_for_the_same_file_is_ranked_as_offered(stack):
    """Spec 0.72 7.7: the pair covers the file; a selector ranks with the current description.

    The same SKILL.md can come with a description shortened or redacted differently. The source
    identity stays the file's hash, and the ranking must follow the description offered now.
    """
    body = "Some neutral body text."
    alpha = skill_file(stack.root, "alpha", "Merge pdf documents", body)
    beta = skill_file(stack.root, "beta", "Write meeting notes", body + " More.")
    task = "merge pdf documents"
    before = stack.until_ranked([alpha, beta], task=task)
    assert before["picks"][0]["name"] == "alpha"
    swapped = [
        dict(alpha, description=beta["description"]),
        dict(beta, description=alpha["description"]),
    ]
    after = stack.until_ranked(swapped, task=task)
    assert after["picks"][0]["name"] == "beta"
    assert {r["content_hash"] for r in stack.offered()} == {
        alpha["content_hash"],
        beta["content_hash"],
    }


def test_switching_a_skill_back_and_forth_between_versions_never_starts_the_wait(stack):
    """Spec 0.72 7.7: file versions A, B, A, B of one skill are four switches, never a stuck sync.

    The clock never moves here, so a sync counted as no progress would hold the next one back
    for 30 s and the router would never rank the version offered now.
    """
    notes = notes_skill(stack)
    task = "merge pdf documents"
    bodies = ["Join several PDF files into one document.", "Combine PDF files into a single file."]
    offered = []
    for body in bodies * 2:
        skill = skill_file(stack.root, "pdf-merge", "Merge pdf documents", body)
        answer = stack.until_ranked([skill, notes], task=task, move_clock=False)
        assert answer["picks"][0]["name"] == "pdf-merge"
        offered.append(skill["content_hash"])
        ranked_hash = next(r["content_hash"] for r in stack.offered() if name_of(r) == "pdf-merge")
        assert ranked_hash == skill["content_hash"]
    assert offered[0] == offered[2] and offered[1] == offered[3] and offered[0] != offered[1]
    assert stack.clock.offset == 0.0
