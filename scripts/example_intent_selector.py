#!/usr/bin/env python3
"""A complete third-party selector, using only the Python standard library (RM28, RM35, RM45).

Copy this file to start your own. The gateway POSTs one published SelectionRequestV2 and accepts one
SelectionResultV2 (src/relaymind/selection/schemas/). A selector proposes; the gateway admits.

It shows one simple way to understand intent:
  1. Word match over the request and each asset's name and description. No model, no index.
     A short follow-up is read with what the user said before it.
  2. For a prompt with letters outside the Latin script, a model writes English keywords.
  3. A model picks from the shortlist.
Steps 2 and 3 run only with --model-url, and a failing model leaves the word match answer.

Run it:
  python scripts/example_intent_selector.py --port 8940
  python scripts/example_intent_selector.py --port 8940 --model-url https://host/v1 \\
      --model some-model --api-key-env MY_KEY --reasoning-effort low

Connect it in the launch file. `primary = "http"` is required: without it the URL is unused.
  [guidance]
  enabled = true
  primary = "http"
  name = "example_intent"
  version = "1"
  url = "http://127.0.0.1:8940/select"
"""

import argparse
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAME, VERSION = "example_intent", "1"
SHORTLIST = 40
ANSWER_TOKENS = 300  # A model that thinks first spends its output limit on the thinking.
MAX_BODY = 33 * 1024 * 1024
KEYWORDS = (
    "Rewrite the user's request as 15 to 25 English search keywords: the action, the object, the "
    "product names and common synonyms. Output only the keywords, separated by spaces."
)
PICK = (
    "You choose which skills and tools an AI assistant needs for a user's request. You get a "
    "numbered list, and many items can look alike: compare what makes them different with the "
    'clues in the request. Reply with JSON only: {"picks":[numbers]}, best first. Find each '
    "separate task in the request and pick the best item for each. A simple request needs one "
    'item; a big workflow needs several. If nothing fits, reply {"picks":[]}.'
)


NO_SPACES = r"[぀-ヿ㐀-鿿가-힯]+"  # Kana, CJK and Hangul: written without spaces.


def words(text):
    """Lowercase words of any script, plus character pairs for scripts written without spaces."""
    text = unicodedata.normalize("NFKC", text).casefold()
    found = set()
    for word in re.findall(r"[^\W_]+", text):
        found.update(re.split(NO_SPACES, word))
    found.discard("")
    for run in re.findall(NO_SPACES, text):
        found.update(run[i : i + 2] for i in range(max(1, len(run) - 1)))
    return found


def strip_thinking(text):
    """The answer without its thinking. A block cut off by the output limit has no closing tag."""
    return re.sub(r"<think>.*?(?:</think>|$)", "", text, flags=re.S).strip()


def first_json_object(text, key):
    """The first JSON object in text that has key, wherever it starts and whatever follows it."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict) and key in value:
            return value
    return None


def needs_keywords(text):
    return any(
        unicodedata.category(c).startswith("L") and not unicodedata.name(c, "").startswith("LATIN")
        for c in text
    )


def label(record):
    identity = record["identity"]
    return identity.get("exposed_name") or identity.get("package_id") or record["asset_id"]


def rank(catalog, query):
    """Best first. A word few assets have counts for more than a common word."""
    overlaps = [query & words(f"{label(r)} {r['description']}") for r in catalog]
    frequency = {}
    for overlap in overlaps:
        for word in overlap:
            frequency[word] = frequency.get(word, 0) + 1
    scored = [
        (sum(math.log(1 + len(catalog) / frequency[w]) for w in overlap), i)
        for i, overlap in enumerate(overlaps)
        if overlap
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [(catalog[i], score) for score, i in scored]


def select(value, chat=None, pick=True):
    """One SelectionRequestV2 to one SelectionResultV2. chat(system, user, max_tokens) -> text."""
    started = time.monotonic()
    if value["schema_version"] != 2:
        raise ValueError("this example speaks selection v2")
    text = value["task_text"]
    catalog = value["catalog"]
    degraded = False
    # The gateway sends what the user said before as the conversation. A short follow-up, such
    # as "yes, do that", is read with it; a longer prompt is a request of its own.
    earlier = "\n".join(
        turn["text"] for turn in value.get("conversation") or [] if turn.get("role") == "user"
    )
    query = words(text)
    follow_up = bool(earlier) and len(text.split()) <= 6 and len(text) <= 40
    if follow_up:
        query |= words(earlier)
    # A follow-up such as "yes, do that" says nothing alone, so its keywords come from both.
    request = f"{earlier}\n{text}" if follow_up else text
    if chat is not None and needs_keywords(request):
        try:
            query |= words(chat(KEYWORDS, request, ANSWER_TOKENS))
        except Exception:  # A failing model leaves the word match answer, and the result says so.
            degraded = True
    ranked = rank(catalog, query)
    shortlist = ranked[:SHORTLIST]
    if chat is not None and pick and shortlist:
        listing = "\n".join(
            f"{i + 1}. {label(r)}: {' '.join(r['description'].split())[:400]}"
            for i, (r, _) in enumerate(shortlist)
        )
        try:
            context = f"Earlier in this conversation:\n{earlier}\n\n" if earlier else ""
            answer = chat(PICK, f"List:\n{listing}\n\n{context}Request:\n{text}", ANSWER_TOKENS)
            numbers = first_json_object(answer, "picks")["picks"]
            chosen, seen = [], set()
            for number in numbers:
                if type(number) is int and 1 <= number <= len(shortlist) and number not in seen:
                    seen.add(number)
                    chosen.append(shortlist[number - 1])
            if numbers and not chosen:
                raise ValueError("the answer names no item of the list")
            # The model's order is the rank, so scores fall with it.
            ranked = [
                (record, float(len(chosen) - rank)) for rank, (record, _) in enumerate(chosen)
            ]
        except Exception:  # A broken answer leaves the word match order, and the result says so.
            degraded = True
    candidates = [
        dict(
            asset_id=record["asset_id"],
            kind=record["kind"],
            content_hash=record["content_hash"],
            source_version=record["source_version"],
            score=float(score),
            score_kind="example_score",
            evidence_refs=["/description"],
        )
        for record, score in ranked[: value["candidate_limit"]]
    ]
    return dict(
        schema_version=2,
        recommendation_id=value["recommendation_id"],
        catalog_revision=value["catalog_revision"],
        host_snapshot_revision=value["host_snapshot_revision"],
        selector=dict(name=NAME, version=VERSION),
        # A benchmark counts a "_degraded" result as failed, so a report cannot claim a model arm
        # that fell back to word match.
        mechanism="example_intent_rank" + ("_degraded" if degraded else ""),
        outcome="ranked" if candidates else "no_match",
        decision="choice_required" if candidates else "no_match",
        candidates=candidates,
        index_revision=None,
        elapsed_ms=(time.monotonic() - started) * 1000,
        usage_claims=None,
    )


def model_client(url, model, api_key_env, reasoning_effort=None):
    """An OpenAI-compatible chat function over the standard library."""

    def chat(system, user, max_tokens):
        body = dict(
            messages=[dict(role="system", content=system), dict(role="user", content=user)],
            max_tokens=max_tokens,
            temperature=0,
        )
        if model:
            body["model"] = model
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        headers = {"Content-Type": "application/json"}
        if api_key_env:
            headers["Authorization"] = f"Bearer {os.environ[api_key_env]}"
        request = urllib.request.Request(
            f"{url.rstrip('/')}/chat/completions", json.dumps(body).encode(), headers
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
        return strip_thinking(data["choices"][0]["message"]["content"] or "")

    return chat


def handler(chat=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # Never log a prompt.
            pass

        def do_POST(self):
            size = int(self.headers.get("content-length", 0))
            if self.path != "/select" or size > MAX_BODY:
                self.send_error(404 if self.path != "/select" else 413)
                return
            try:
                body = json.dumps(select(json.loads(self.rfile.read(size)), chat=chat)).encode()
                status = 200
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
    parser.add_argument("--port", type=int, default=8940)
    parser.add_argument("--model-url", help="OpenAI-compatible /v1 root. Unset: word match only.")
    parser.add_argument("--model")
    parser.add_argument("--api-key-env")
    parser.add_argument(
        "--reasoning-effort", help="Sent as is, for a model that thinks by default."
    )
    args = parser.parse_args()
    if args.model_url and not args.model:
        parser.error("--model-url needs --model")
    if args.api_key_env and not os.environ.get(args.api_key_env):
        parser.error(f"{args.api_key_env} is not set: every model call would fail")
    chat = (
        model_client(args.model_url, args.model, args.api_key_env, args.reasoning_effort)
        if args.model_url
        else None
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(chat))
    print(f"Serving on http://127.0.0.1:{args.port}/select", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
