"""Guidance selector feedback (spec 0.75 section 7.7; RM48, AC43).

After a turn, the host plugin says which offered assets the turn used. The gateway maps them to the
pairs of that select and, after it answers, sends them to the selector, which already holds the
request under its `recommendation_id`. Feedback carries no text or path, goes only to a selector
with local inference unless the operator allows remote, and changes no answer: any failure ends it
quietly and is counted.
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import httpx

from relaymind.schema_util import StrictValidator, first_schema_error, load_packaged_schema
from relaymind.selection.http import validate_url

from .pipeline import GuidanceSettings
from .sync import FEEDER_ID, GuidanceSync, exchange

# Remembered selects, the oldest dropped first, each for at most TTL_S (spec 0.75).
MAX_IDS = 1024
TTL_S = 3600.0
# Distinct offers kept once each, at most one per remembered select: above the offer limit every
# select is its own offer. An ID whose offer was dropped is forgotten too.
MAX_OFFERS = MAX_IDS
# The shared table of offered pairs is rebuilt from the kept offers when it grows past this.
MAX_PAIRS = 100_000
# A remembered remote inference is asked again after this long, so a selector restarted with
# local inference gets feedback again even if it never answers not_ready (spec 0.75).
ASK_AGAIN_S = 600.0
# Feedbacks waiting behind the one being sent; one more is dropped.
MAX_WAITING = 16
MAX_FEEDBACK_BYTES = 1024 * 1024
MAX_NAME_BYTES = 128
_REQUEST = StrictValidator(load_packaged_schema("relaymind.guidance", "guidance_feedback.v1.json"))


def parse_feedback_request(value: object) -> dict:
    """A closed GuidanceFeedbackV1, or ValueError."""
    if first_schema_error(_REQUEST, value) is not None:
        raise ValueError("invalid_request")
    keys = [(item["kind"], item["name"]) for item in value["used"]]
    if len(set(keys)) != len(keys) or any(
        len(name.encode("utf-8")) > MAX_NAME_BYTES for _, name in keys
    ):
        raise ValueError("invalid_request")
    return value


@dataclass(frozen=True)
class FeedbackJob:
    """One feedback to send: the used pairs, how many used assets the select did not offer, and
    every offered pair for asking the inference."""

    recommendation_id: str
    used: list[dict]
    not_offered: int
    offered: list[dict]


class GuidanceFeedback:
    def __init__(
        self,
        settings: GuidanceSettings,
        sync: GuidanceSync,
        *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.url = validate_url(settings.feedback_url)
        self.allow_remote = settings.sync_allow_remote
        self.exchange_s = settings.sync_ms / 1000
        self._sync = sync
        self._transport_factory = transport_factory
        self._clock = clock
        # Per offer, its (asset ID, content hash) pairs. Each pair object is shared through
        # `_pairs`, so many large offers that differ a little cost little (spec 0.75).
        self._offers: OrderedDict[int, tuple[tuple[str, str], ...]] = OrderedDict()
        self._pairs: dict[tuple[str, str], tuple[str, str]] = {}
        # Per remembered select: its offer and when it is forgotten.
        self._ids: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._lock = asyncio.Lock()
        # Feedbacks waiting for the lock: counted until each takes it, so the count holds also
        # between a release and the next waiter taking the lock, when the lock reads as free.
        self._waiting = 0
        self._counts = dict(feedback_sent=0, feedback_failed=0, feedback_skipped=0)
        self._ask_again_at = 0.0

    def status(self) -> dict:
        """The feedback fields of GuidanceSyncStatusV1: counts since start, no asset or text."""
        return dict(feedback_enabled=True, **self._counts)

    def remember(self, recommendation_id: str, revision: int, catalog: Sequence[Mapping]) -> None:
        """Keep the offered pairs of a select the selector answered (spec 0.75)."""
        if revision in self._offers:
            self._offers.move_to_end(revision)
        else:
            if len(self._pairs) > MAX_PAIRS:  # Keep only the pairs of offers still kept.
                self._pairs = {pair: pair for offer in self._offers.values() for pair in offer}
            self._offers[revision] = tuple(
                self._pairs.setdefault(pair, pair)
                for pair in ((r["asset_id"], r["content_hash"]) for r in catalog)
            )
            while len(self._offers) > MAX_OFFERS:
                self._offers.popitem(last=False)
        self._ids[recommendation_id] = (revision, self._clock() + TTL_S)
        self._ids.move_to_end(recommendation_id)
        while len(self._ids) > MAX_IDS:
            self._ids.popitem(last=False)

    def take(self, request: Mapping) -> FeedbackJob | None:
        """The job for a parsed GuidanceFeedbackV1, or None when its select is not remembered,
        which counts as skipped, so lost feedback shows in the status (spec 0.75).

        A select is forgotten once taken, so it gets at most one feedback."""
        remembered = self._ids.pop(request["recommendation_id"], None)
        offer = None
        if remembered is not None and remembered[1] > self._clock():
            offer = self._offers.get(remembered[0])
        if offer is None:
            self._counts["feedback_skipped"] += 1
            return None
        hashes = dict(offer)
        used = []
        for item in request["used"]:
            # The asset ID of the select record (pipeline._record); one not offered is counted.
            asset_id = f"host:{item['kind']}:{item['name']}"
            if asset_id in hashes:
                used.append(
                    dict(
                        asset_id=asset_id,
                        content_hash=hashes[asset_id],
                        kind=item["kind"],
                        signal=item["signal"],
                    )
                )
        offered = [dict(asset_id=a, content_hash=h) for a, h in offer]
        not_offered = len(request["used"]) - len(used)
        return FeedbackJob(request["recommendation_id"], used, not_offered, offered)

    async def send(self, job: FeedbackJob) -> None:
        """Send one feedback after the answer, one at a time. Never raises (a cancellation does)."""
        if self._waiting >= MAX_WAITING:  # A feedback that finds 16 others waiting is dropped.
            self._counts["feedback_skipped"] += 1
            return
        self._waiting += 1
        try:
            await self._lock.acquire()
        finally:
            self._waiting -= 1
        try:
            outcome = await self._send(job)
        except Exception:  # The answer is already sent; a failed feedback changes nothing.
            outcome = "feedback_failed"
        finally:
            self._lock.release()
        self._counts[outcome] += 1

    async def _send(self, job: FeedbackJob) -> str:
        if not self.allow_remote:
            # Asked only when it decides something: an operator who allows remote never waits.
            # A remembered remote is asked again after ASK_AGAIN_S. None while a sync runs or
            # waits after a failure: this feedback is skipped.
            inference = self._sync.inference
            if inference is None or (inference == "remote" and self._clock() >= self._ask_again_at):
                self._ask_again_at = self._clock() + ASK_AGAIN_S
                inference = await self._sync.ask_inference(job.offered)
            if inference != "local":
                return "feedback_skipped"
        answer = await exchange(
            self.url,
            dict(
                schema_version=1,
                operation="feedback",
                feeder_id=FEEDER_ID,
                recommendation_id=job.recommendation_id,
                used=job.used,
                not_offered=job.not_offered,
            ),
            seconds=self.exchange_s,
            clock=self._clock,
            transport_factory=self._transport_factory,
        )
        self._sync.inference = answer["inference"]
        return "feedback_sent"
