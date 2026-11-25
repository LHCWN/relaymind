"""Guidance selector sync (spec 0.72 section 7.7; RM47, AC42).

After an answer whose third-party selector said `not_ready`, the gateway tells the selector which
records the host offered and uploads only the ones it is missing. A skill body reaches the
selector only as the plugin's file path, which the selector reads itself. The gateway never reads,
hashes or logs a body, and a sync changes no answer: any failure ends it quietly.
"""

import asyncio
import hashlib
import math
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence

import httpx

from relaymind.canonical import canonical_json
from relaymind.schema_util import StrictValidator, first_schema_error, load_packaged_schema
from relaymind.selection.asset_contract import SelectorContext
from relaymind.selection.asset_http import post_json
from relaymind.selection.http import validate_url

FEEDER_ID = "guidance"
# Encoding up to 1,000 records takes a while; above this many, other requests keep the loop.
_THREAD_ABOVE = 200
# Pairs a selector rejected without a file; the oldest is dropped first (spec 0.72).
MAX_REJECTED = 10_000
# Asset IDs whose current target is remembered: the selector keeps one version of each asset, so
# only the last accepted record of an ID tells a stuck upload from a switch (spec 0.72).
MAX_TARGETS = 10_000
# After a failed sync none starts for this long, doubled for each failure in a row, up to the cap.
FIRST_WAIT_S = 30.0
MAX_WAIT_S = 600.0
_RESPONSE = StrictValidator(
    load_packaged_schema("relaymind.guidance", "asset_sync_response.v1.json")
)


def _key(record: Mapping) -> tuple[str, str]:
    return record["asset_id"], record["content_hash"]


def _target(record: Mapping) -> str:
    """The version a record makes current: the SHA-256 of the exact record, its hash included."""
    return hashlib.sha256(canonical_json(record).encode()).hexdigest()


async def exchange(
    url: str,
    document: Mapping,
    *,
    seconds: float,
    clock: Callable[[], float],
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> dict:
    """One selector exchange: a valid AssetSyncResponseV1 of the same operation, or an error."""
    if len(document.get("assets", ())) > _THREAD_ABOVE:
        payload = (await asyncio.to_thread(canonical_json, document)).encode()
    else:
        payload = canonical_json(document).encode()
    # wait_for bounds the whole exchange; the transport needs no clock of its own.
    answer = await asyncio.wait_for(
        post_json(
            url, payload, SelectorContext(clock, lambda: None), transport_factory=transport_factory
        ),
        seconds,
    )
    if (
        first_schema_error(_RESPONSE, answer) is not None
        or answer["operation"] != document["operation"]
    ):
        raise ValueError("invalid_sync_response")
    return answer


class GuidanceSync:
    """At most one sync at a time; a sync wanted while one runs is skipped."""

    def __init__(
        self,
        settings,
        *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.url = validate_url(settings.sync_url)
        self.allow_remote = settings.sync_allow_remote
        self.exchange_s = settings.sync_ms / 1000
        self._transport_factory = transport_factory
        self._clock = clock
        self._running = False
        self._failures = 0
        self._not_before = 0.0
        self._rejected: OrderedDict[tuple[str, str], None] = OrderedDict()
        # Per asset ID, the target the selector last accepted; the least recently updated is first.
        self._targets: OrderedDict[str, str] = OrderedDict()
        # The last sync whose find_missing was answered: counts only, never an asset (RM16, RM47).
        # A running sync fills `_current`; the status shows it only once that sync ends.
        self._current: dict | None = None
        self._last = self._nothing()
        # The last inference a selector exchange reported, or None before any (spec 0.75; RM48).
        self.inference: str | None = None

    def status(self) -> dict:
        """GuidanceSyncStatusV1 for `GET /relay/v1/guidance/sync`."""
        wait_ms = math.ceil(max(0.0, self._not_before - self._clock()) * 1000)
        return dict(
            schema_version=1,
            enabled=True,
            running=self._running,
            **self._last,
            consecutive_failures=self._failures,
            retry_after_ms=wait_ms,
        )

    async def run(self, catalog: Sequence[Mapping], files: Mapping[str, Mapping]) -> None:
        """Sync the offered records; `files` holds each skill file's path and size by asset ID.

        Never raises (a cancellation goes through)."""
        if self._running or self._clock() < self._not_before:
            return
        self._running, self._current = True, None
        try:
            progress = await self._sync(catalog, files)
        except Exception:  # The answer is already sent; a failed sync changes nothing.
            # Without an answered find_missing this sync counted nothing (spec 0.72).
            self._last = dict(self._current or self._nothing(), last_outcome="failed")
            self._wait()
        else:
            self._last = self._current
            if progress:
                self._failures, self._not_before = 0, 0.0
            else:  # Asking on every not_ready would get the same answer again.
                self._wait()
        finally:
            self._running, self._current = False, None

    async def ask_inference(self, pairs: list[dict]) -> str:
        """Learn the selector's inference with a find_missing of these pairs, uploading nothing.

        Feedback needs it before it sends anything (spec 0.75; RM48). Raises on any failure."""
        _, inference = await self._find_missing(pairs)
        return inference

    async def _find_missing(self, pairs: list[dict]) -> tuple[set[tuple[str, str]], str]:
        """The missing pairs and the inference of a valid find_missing answer, else an error.

        Only a valid answer, whose missing pairs were offered and do not repeat, teaches the
        inference (spec 0.75)."""
        found = await self._exchange("find_missing", pairs)
        missing = [_key(pair) for pair in found["missing"]]
        chosen = set(missing)
        if len(chosen) != len(missing) or not chosen <= {_key(pair) for pair in pairs}:
            raise ValueError("invalid_sync_response")
        self.inference = found["inference"]
        return chosen, found["inference"]

    @staticmethod
    def _nothing() -> dict:
        return dict(
            last_outcome="none",
            last_inference=None,
            missing=0,
            accepted=0,
            rejected=0,
            files_sent=0,
        )

    def _wait(self) -> None:
        self._failures += 1
        wait = min(MAX_WAIT_S, FIRST_WAIT_S * 2 ** min(self._failures - 1, 10))
        self._not_before = self._clock() + wait

    async def _sync(self, catalog: Sequence[Mapping], files: Mapping[str, Mapping]) -> bool:
        """One sync; False when it made no progress (spec 0.72): every record it uploaded is the
        current target of its asset ID, or every missing pair was rejected before."""
        chosen, inference = await self._find_missing(
            [dict(asset_id=r["asset_id"], content_hash=r["content_hash"]) for r in catalog]
        )
        known = chosen & self._rejected.keys()
        self._current = current = dict(
            last_outcome="nothing_missing" if not chosen else "synced",
            last_inference=inference,
            missing=len(chosen),
            accepted=0,
            rejected=len(known),
            files_sent=0,
        )
        # A pair rejected without a file would be rejected again; a new hash is a new pair.
        chosen -= known
        if not chosen:
            return not known
        records = [record for record in catalog if _key(record) in chosen]
        # A selector reports a pair whose indexing failed as missing again (spec 0.72): sending its
        # current target again is no progress. Any other record switches the asset's version, a
        # return to an earlier one included (hashes A, B, A), and that is progress.
        progress = any(self._targets.get(r["asset_id"]) != _target(r) for r in records)
        # A selector that sends text to remote inference gets no file unless the operator allows it.
        send_files = inference == "local" or self.allow_remote
        uploads = [
            dict(
                record=record,
                content=dict(kind="file", **files[record["asset_id"]])
                if send_files and record["asset_id"] in files
                else None,
            )
            for record in records
        ]
        current["files_sent"] = sum(1 for item in uploads if item["content"])
        rejected = await self._upload(uploads)
        # A pair the selector could not take with its file may still be indexed from its record.
        again = [
            dict(item, content=None)
            for item in uploads
            if item["content"] and _key(item["record"]) in rejected
        ]
        retrying = {_key(item["record"]) for item in again}
        sent = {_key(record): record for record in records}
        self._remember(current, sent, chosen - rejected, rejected - retrying)
        if again:
            rejected = await self._upload(again)
            self._remember(current, sent, retrying - rejected, rejected)
        return progress

    def _remember(self, current: dict, sent: Mapping, accepted: set, rejected: set) -> None:
        """Keep confirmed results even if a later upload in the same sync fails.

        `sent` maps each uploaded pair to its record: an accepted record becomes the target of its
        asset ID, and a rejection leaves that ID with no known target."""
        current["accepted"] += len(accepted)
        current["rejected"] += len(rejected)
        for pair in rejected:
            self._rejected[pair] = None
            self._targets.pop(pair[0], None)
        for pair in accepted:
            self._targets[pair[0]] = _target(sent[pair])
            self._targets.move_to_end(pair[0])
        while len(self._rejected) > MAX_REJECTED:
            self._rejected.popitem(last=False)
        while len(self._targets) > MAX_TARGETS:
            self._targets.popitem(last=False)

    async def _upload(self, uploads: list[dict]) -> set[tuple[str, str]]:
        """Upload, and return the pairs the selector rejected. One result for each pair sent."""
        answer = await self._exchange("upload", uploads)
        results = [_key(result) for result in answer["results"]]
        if len(set(results)) != len(results) or set(results) != {
            _key(i["record"]) for i in uploads
        }:
            raise ValueError("invalid_sync_response")
        return {_key(r) for r in answer["results"] if r["status"] == "rejected"}

    async def _exchange(self, operation: str, assets: list) -> dict:
        document = dict(schema_version=1, operation=operation, feeder_id=FEEDER_ID, assets=assets)
        return await exchange(
            self.url,
            document,
            seconds=self.exchange_s,
            clock=self._clock,
            transport_factory=self._transport_factory,
        )
