#!/usr/bin/env python3
"""Where an embedding model ranks the right skill, by embedding alone (RM45, RM47; spec 0.74 7.7).

For each one-skill prompt of the similar-skill data, it embeds the prompt and every skill's
description of the split, as asset-router does, and reports where the right skill ranks and its
cosine. A selector's threshold, lexical stage or fallback play no part, so this shows what the
model itself can tell apart and on what scale it scores. Prompts are grouped as English, Chinese
with an English word, and pure Chinese.

  python scripts/guidance_embedding_probe.py --url http://127.0.0.1:8950 --output probe.json

The endpoint is OpenAI-compatible (`/v1/embeddings`), such as llama.cpp's server, on loopback
only. Development evidence: the data and its answers were written by a model.
"""

import argparse
import json
import re
import statistics
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from relaymind.adapters.local_http import is_loopback_url  # noqa: E402

DATA = ROOT / "evaluation/assets/guidance-similar/similar.v1.json"

Embed = Callable[[list[str]], list[list[float]]]


def group_of(chinese: str) -> str:
    """A Chinese prompt with an English word (two Latin letters or more) is mixed."""
    return "zh_mixed" if re.search(r"[A-Za-z]{2,}", chinese) else "zh_pure"


def _unit(vector: list[float]) -> list[float]:
    norm = sum(x * x for x in vector) ** 0.5
    return [x / norm for x in vector] if norm else vector


def _summary(cosines: list[float], ranks: list[int]) -> dict:
    return dict(
        prompts=len(ranks),
        rank_1=sum(r == 1 for r in ranks),
        top_3=sum(r <= 3 for r in ranks),
        right_cosine=dict(
            min=round(min(cosines), 4),
            median=round(statistics.median(cosines), 4),
            max=round(max(cosines), 4),
        ),
    )


def probe(
    data: dict, split: str, embed: Embed, query_prefix: str = "", document_prefix: str = ""
) -> dict:
    """Rank and cosine of the right skill per group, over the split's one-skill prompts."""
    skills = [skill for family in data["splits"][split] for skill in family["skills"]]
    documents = [_unit(v) for v in embed([document_prefix + s["description"] for s in skills])]
    prompts = [("en", i, s["en"]) for i, s in enumerate(skills)]
    prompts += [(group_of(s["zh"]), i, s["zh"]) for i, s in enumerate(skills)]
    queries = [_unit(v) for v in embed([query_prefix + text for _, _, text in prompts])]
    found: dict[str, tuple[list[float], list[int]]] = {}
    for (group, right, _), query in zip(prompts, queries, strict=True):
        scores = [sum(a * b for a, b in zip(query, d, strict=True)) for d in documents]
        cosines, ranks = found.setdefault(group, ([], []))
        cosines.append(scores[right])
        ranks.append(1 + sum(score > scores[right] for score in scores))
    order = ("en", "zh_mixed", "zh_pure")
    return {g: _summary(*found[g]) for g in order if g in found}


def http_embedder(url: str, model: str, batch: int = 8, timeout_s: float = 900) -> Embed:
    endpoint = url.rstrip("/") + "/v1/embeddings"
    # No proxy for loopback: a proxy from the environment would take prompts off the machine.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def embed(texts: list[str]) -> list[list[float]]:
        vectors = []
        for start in range(0, len(texts), batch):
            body = json.dumps(dict(model=model, input=texts[start : start + batch])).encode()
            request = urllib.request.Request(
                endpoint, data=body, headers={"Content-Type": "application/json"}
            )
            with opener.open(request, timeout=timeout_s) as response:
                rows = json.load(response)["data"]
            vectors += [row["embedding"] for row in sorted(rows, key=lambda r: r["index"])]
        return vectors

    return embed


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="Embeddings server on loopback.")
    parser.add_argument("--model", default="embedding", help="Model name sent to the server.")
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--split", choices=("development", "holdout"), default="holdout")
    parser.add_argument("--query-prefix", default="", help="Put before each prompt.")
    parser.add_argument("--document-prefix", default="", help="Put before each description.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not is_loopback_url(args.url):
        parser.error("--url must be on loopback, so no prompt leaves the machine")
    data = json.loads(args.data.read_text("utf-8"))
    groups = probe(
        data,
        args.split,
        http_embedder(args.url, args.model),
        args.query_prefix,
        args.document_prefix,
    )
    result = dict(
        split=args.split,
        model=args.model,
        query_prefix=args.query_prefix,
        document_prefix=args.document_prefix,
        groups=groups,
    )
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.output:
        args.output.write_text(text + "\n", "utf-8")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
