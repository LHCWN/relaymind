"""POST /relay/v1/guidance: stateless per-prompt guidance (RM45, AC38; spec section 7.7).

Also the selector sync status (RM47) and selector feedback (RM48, spec 0.75). Nothing is written to
the store. The request body and the prompt are not logged.
"""

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from relaymind.api.recommendations import guarded_route
from relaymind.guidance.feedback import MAX_FEEDBACK_BYTES, parse_feedback_request
from relaymind.guidance.pipeline import (
    MAX_GUIDANCE_BYTES,
    GuidanceSettings,
    parse_guidance_request,
    run_guidance,
)
from relaymind.recommendations.errors import RecommendationError
from relaymind.selection.asset_http import strict_json

ENDPOINT = "/relay/v1/guidance"
SYNC_STATUS = "/relay/v1/guidance/sync"
FEEDBACK = "/relay/v1/guidance/feedback"
# The status without a sync URL: nothing configured, nothing counted (spec 0.72).
_NO_SYNC = dict(
    schema_version=1,
    enabled=False,
    running=False,
    last_outcome="none",
    last_inference=None,
    missing=0,
    accepted=0,
    rejected=0,
    files_sent=0,
    consecutive_failures=0,
    retry_after_ms=0,
)
# The feedback fields without a feedback URL (spec 0.75; RM48).
_NO_FEEDBACK = dict(feedback_enabled=False, feedback_sent=0, feedback_failed=0, feedback_skipped=0)


async def _body(request: Request, limit: int | None = None) -> bytes:
    limit = MAX_GUIDANCE_BYTES if limit is None else limit
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise RecommendationError("invalid_request")
        chunks.append(chunk)
    return b"".join(chunks)


def install_guidance_routes(
    app: FastAPI,
    settings: GuidanceSettings,
    *,
    keywords=None,
    picker=None,
    selector=None,
    sync=None,
    feedback=None,
    redact=lambda text: text,
) -> None:
    """`sync` is a GuidanceSync, or None when `guidance.sync_url` is not set (RM47); `feedback` a
    GuidanceFeedback, or None when `guidance.feedback_url` is not set (RM48)."""

    async def guidance(request: Request):
        if not settings.enabled:
            raise RecommendationError("recommendation_unavailable")
        raw = await _body(request)
        try:
            # Checking up to 20,000 assets takes a while; other requests keep the event loop.
            parsed = await asyncio.to_thread(lambda: parse_guidance_request(strict_json(raw)))
        except (ValueError, TypeError, RecursionError):  # GuidanceError is a ValueError.
            raise RecommendationError("invalid_request") from None
        later = None

        def sync_after(catalog, files):
            nonlocal later
            # The sync runs after the answer is sent, so a turn never waits for it (spec 0.72).
            later = BackgroundTask(sync.run, catalog, files)

        answer = await run_guidance(
            parsed,
            settings,
            keywords=keywords,
            picker=picker,
            selector=selector,
            redact=redact,
            on_sync=None if sync is None else sync_after,
            on_selected=None if feedback is None else feedback.remember,
        )
        return JSONResponse(answer, background=later)

    async def sync_status(request: Request):
        if not settings.enabled:
            raise RecommendationError("recommendation_unavailable")
        found = _NO_SYNC if sync is None else sync.status()
        return JSONResponse({**found, **(_NO_FEEDBACK if feedback is None else feedback.status())})

    async def take_feedback(request: Request):
        if not settings.enabled:
            raise RecommendationError("recommendation_unavailable")
        raw = await _body(request, MAX_FEEDBACK_BYTES)
        try:
            parsed = parse_feedback_request(strict_json(raw))
        except (ValueError, TypeError, RecursionError):
            raise RecommendationError("invalid_request") from None
        job = None if feedback is None else feedback.take(parsed)
        # Sent after the answer, so the host never waits for the selector (spec 0.75).
        later = None if job is None else BackgroundTask(feedback.send, job)
        return JSONResponse(dict(schema_version=1, queued=job is not None), background=later)

    app.add_api_route(ENDPOINT, guarded_route(app, guidance), methods=["POST"])
    app.add_api_route(SYNC_STATUS, guarded_route(app, sync_status), methods=["GET"])
    app.add_api_route(FEEDBACK, guarded_route(app, take_feedback), methods=["POST"])
