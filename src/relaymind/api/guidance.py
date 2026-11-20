"""POST /relay/v1/guidance: stateless per-prompt guidance (RM45, AC38; spec section 7.7).

Nothing is written to the store. The request body and the prompt are not logged.
"""

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from relaymind.api.recommendations import guarded_route
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


async def _body(request: Request) -> bytes:
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_GUIDANCE_BYTES:
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
    redact=lambda text: text,
) -> None:
    """`sync` is a GuidanceSync, or None when `guidance.sync_url` is not set (RM47)."""

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
            on_not_ready=None if sync is None else sync_after,
        )
        return JSONResponse(answer, background=later)

    async def sync_status(request: Request):
        if not settings.enabled:
            raise RecommendationError("recommendation_unavailable")
        return JSONResponse(_NO_SYNC if sync is None else sync.status())

    app.add_api_route(ENDPOINT, guarded_route(app, guidance), methods=["POST"])
    app.add_api_route(SYNC_STATUS, guarded_route(app, sync_status), methods=["GET"])
