#!/usr/bin/env python3
"""Serve the built-in guidance stages as a selection v2 service for the benchmark (RM40, RM45).

This lets scripts/source_asset_benchmark.py run and compare the built-in stages exactly like any
third-party selector, on the same prompts through the same gateway path:

  python scripts/guidance_selector_server.py --port 8941 --name guidance_local \\
      --keywords local --local-url http://127.0.0.1:8033

External stages read RELAYMIND_EXTERNAL_BASE_URL, RELAYMIND_EXTERNAL_MODEL and
RELAYMIND_EXTERNAL_API_KEY. Development tooling: it is not part of a running gateway.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from relaymind.guidance import GuidanceSettings, parse_guidance_request, run_guidance
from relaymind.guidance.models import external_client, local_keyword_client
from relaymind.guidance.pipeline import (
    MAX_DESCRIPTION_BYTES,
    MAX_NAME_BYTES,
    clip_bytes,
    clip_end,
)
from relaymind.recommendations.limits import MAX_TASK_BYTES
from relaymind.selection.asset_contract import parse_asset_request, parse_asset_result

MAX_BODY = 33 * 1024 * 1024


def _label(record):
    identity = record["identity"]
    return identity.get("exposed_name") or identity.get("package_id") or record["asset_id"]


async def select(value, settings, *, name, version, keywords=None, picker=None):
    """One SelectionRequestV2 through run_guidance to one SelectionResultV2."""
    data = parse_asset_request(value).data
    by_key = {}
    assets = []
    for record in data["catalog"]:
        key = (record["kind"], clip_bytes(_label(record), MAX_NAME_BYTES))
        if key in by_key:
            # Two records with one name, as two versions of a skill: each stays a choice.
            # A short digest keeps the name inside the limit, however long the asset ID is.
            digest = hashlib.sha256(record["asset_id"].encode("utf-8")).hexdigest()[:12]
            suffix = f" ({digest})"
            key = (key[0], clip_bytes(key[1], MAX_NAME_BYTES - len(suffix.encode())) + suffix)
            if key in by_key:
                continue
        by_key[key] = record
        description = clip_bytes(record["description"], MAX_DESCRIPTION_BYTES)
        assets.append(dict(name=key[1], kind=key[0], description=description))
    request = dict(schema_version=1, task_text=data["task_text"], assets=assets)
    # The gateway sends what the user said before as the conversation. A follow-up needs it.
    said = [turn["text"] for turn in data.get("conversation") or [] if turn.get("role") == "user"]
    if said:  # The latest words matter most, so a long text keeps its end.
        request["recent_text"] = clip_end("\n".join(said), MAX_TASK_BYTES)
    outcome = await run_guidance(
        parse_guidance_request(request),
        settings,
        keywords=keywords,
        picker=picker,
    )
    candidates = []
    for rank, pick in enumerate(outcome["picks"][: data["candidate_limit"]]):
        record = by_key[(pick["kind"], pick["name"])]
        candidates.append(
            dict(
                asset_id=record["asset_id"],
                kind=record["kind"],
                content_hash=record["content_hash"],
                source_version=record["source_version"],
                score=float(len(outcome["picks"]) - rank),
                score_kind="guidance_rank",
                evidence_refs=["/description"],
            )
        )
    elapsed = sum(stage["ms"] for stage in outcome["stages"])
    result = dict(
        schema_version=2,
        recommendation_id=data["recommendation_id"],
        catalog_revision=data["catalog_revision"],
        host_snapshot_revision=data["host_snapshot_revision"],
        selector=dict(name=name, version=version),
        # A failed stage must show in the benchmark evidence, not read as the configured arm.
        mechanism="guidance_"
        + outcome["mechanism"].replace("+", "_")
        + ("_degraded" if outcome["degraded"] else ""),
        outcome="ranked" if candidates else "no_match",
        decision="choice_required" if candidates else "no_match",
        candidates=candidates,
        index_revision=None,
        elapsed_ms=elapsed,
        usage_claims=None,
    )
    return parse_asset_result(result).to_json_object()


def make_handler(answer):
    """A request handler for POST /select; answer(request) gives the result."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # Never log a prompt.
            pass

        def do_POST(self):
            size = int(self.headers.get("content-length", 0))
            if self.path != "/select" or size > MAX_BODY:
                self.send_error(404 if self.path != "/select" else 413)
                return
            try:
                body, status = json.dumps(answer(json.loads(self.rfile.read(size)))).encode(), 200
            except Exception:
                body, status = b"{}", 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--version", default="1")
    parser.add_argument("--keywords", choices=("none", "local", "external"), default="none")
    parser.add_argument("--picker", choices=("none", "external"), default="none")
    parser.add_argument("--local-url", default="http://127.0.0.1:8033")
    parser.add_argument("--total-ms", type=int, default=60000)
    parser.add_argument("--unpicked-limit", type=int, default=8)
    args = parser.parse_args()
    settings = GuidanceSettings(
        enabled=True,
        keywords=args.keywords,
        picker=args.picker,
        total_ms=args.total_ms,
        unpicked_limit=args.unpicked_limit,
    )
    external = None
    if "external" in (args.keywords, args.picker):
        needed = (
            "RELAYMIND_EXTERNAL_BASE_URL",
            "RELAYMIND_EXTERNAL_MODEL",
            # Without the key every model call fails and each case falls back to word match.
            "RELAYMIND_EXTERNAL_API_KEY",
        )
        if missing := [name for name in needed if not os.environ.get(name)]:
            parser.error(f"an external stage needs {', '.join(missing)} in the environment")
        external = external_client(
            SimpleNamespace(
                base_url=os.environ["RELAYMIND_EXTERNAL_BASE_URL"],
                model=os.environ["RELAYMIND_EXTERNAL_MODEL"],
                api_key_env="RELAYMIND_EXTERNAL_API_KEY",
                request_options={"reasoning_effort": "low"},
            )
        )
    local = local_keyword_client(args.local_url) if args.keywords == "local" else None
    keywords = {"local": local, "external": external, "none": None}
    picker = external if args.picker == "external" else None

    def answer(value):
        return asyncio.run(
            select(
                value,
                settings,
                name=args.name,
                version=args.version,
                keywords=keywords[args.keywords],
                picker=picker,
            )
        )

    print(f"Serving on http://127.0.0.1:{args.port}/select", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(answer)).serve_forever()


if __name__ == "__main__":
    main()
