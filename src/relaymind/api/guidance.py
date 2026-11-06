"""POST /relay/v1/guidance: stateless per-prompt guidance (RM45, AC38; spec section 7.7).

Nothing is written to the store. The request body and the prompt are not logged.
"""

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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
    redact=lambda text: text,
) -> None:
    async def guidance(request: Request):
        if not settings.enabled:
            raise RecommendationError("recommendation_unavailable")
        raw = await _body(request)
        try:
            # Checking up to 20,000 assets takes a while; other requests keep the event loop.
            parsed = await asyncio.to_thread(lambda: parse_guidance_request(strict_json(raw)))
        except (ValueError, TypeError, RecursionError):  # GuidanceError is a ValueError.
            raise RecommendationError("invalid_request") from None
        return JSONResponse(
            await run_guidance(
                parsed,
                settings,
                keywords=keywords,
                picker=picker,
                selector=selector,
                redact=redact,
            )
        )

    app.add_api_route(ENDPOINT, guarded_route(app, guidance), methods=["POST"])
