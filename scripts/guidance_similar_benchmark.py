#!/usr/bin/env python3
"""Can guidance tell similar skills apart from the prompt alone (RM16, RM45, section 7.7)?

Each family holds skills that do nearly the same job and differ in one real way, often late in a
long shared description. The harness offers all families at once, plus optional background assets,
and runs the real guidance stages on prompts in English and Chinese: prompts for one skill, prompts
that fit every skill of a family, short follow-ups, and unrelated prompts.

  python scripts/guidance_similar_benchmark.py --split development --output out.json --arm word
  RELAYMIND_EXTERNAL_API_KEY=... python scripts/guidance_similar_benchmark.py \\
      --split holdout --output out.json --arm external_picker \\
      --background assets.json --model-base-url https://host/v1 --model some-model \\
      --request-option reasoning_effort=low
  python scripts/guidance_similar_benchmark.py --split holdout --output mine.json \\
      --arm third_party --selector-url http://127.0.0.1:8940/select --selector-name mine

Compare the reports with scripts/guidance_compare.py.

Development evidence: a model wrote the data and its expected answers, so this is not acceptance.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import replace
from itertools import cycle
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from relaymind.adapters.chat_completions import check_request_options  # noqa: E402
from relaymind.guidance import GuidanceSettings, parse_guidance_request, run_guidance  # noqa: E402
from relaymind.guidance.models import chat_client, local_keyword_client  # noqa: E402
from relaymind.selection.asset_http import HttpAssetSelector  # noqa: E402

DATA = ROOT / "evaluation/assets/guidance-similar/similar.v1.json"
ARMS = dict(
    word=dict(),
    local=dict(keywords="local"),
    local_picker=dict(keywords="local", picker="external"),
    external_picker=dict(keywords="external", picker="external"),
    third_party=dict(primary="http"),
)


def load_cases(split: str, data_path: Path = DATA) -> tuple[list[dict], list[dict]]:
    """(assets, cases) of one split. A case names its expected skill, or its whole family."""
    data = json.loads(data_path.read_text("utf-8"))
    assets, cases = [], []
    follow_ups = {lane: cycle(data["follow_up"][lane]) for lane in ("en", "zh")}
    for family in data["splits"][split]:
        names = [skill["name"] for skill in family["skills"]]
        for skill in family["skills"]:
            assets.append(dict(name=skill["name"], kind="skill", description=skill["description"]))
            for lane in ("en", "zh"):
                cases.append(
                    dict(
                        case=f"{lane}:{skill['name']}",
                        lane=lane,
                        text=skill[lane],
                        expected=[skill["name"]],
                        family=names,
                    )
                )
        # A short follow-up names nothing, and only the earlier request can decide it.
        first = family["skills"][0]
        for lane in ("en", "zh"):
            cases.append(
                dict(
                    case=f"follow_up_{lane}:{first['name']}",
                    lane=f"follow_up_{lane}",
                    text=next(follow_ups[lane]),
                    recent=first[lane],
                    expected=[first["name"]],
                    family=names,
                )
            )
        for lane in ("en", "zh"):
            cases.append(
                dict(
                    case=f"no_signal_{lane}:{family['family']}",
                    lane=f"no_signal_{lane}",
                    text=family[f"no_signal_{lane}"],
                    expected=names,
                    family=names,
                )
            )
    # A big workflow needs its whole set of skills.
    for number, request in enumerate(data["complex"][split]):
        for lane in ("en", "zh"):
            cases.append(
                dict(
                    case=f"complex_{lane}:{number}",
                    lane=f"complex_{lane}",
                    text=request[lane],
                    expected=request["skills"],
                    family=request["skills"],
                )
            )
    for lane in ("en", "zh"):
        for number, text in enumerate(data["unrelated"][lane]):
            cases.append(
                dict(
                    case=f"unrelated_{lane}:{number}",
                    lane=f"unrelated_{lane}",
                    text=text,
                    expected=[],
                    family=[],
                )
            )
    return assets, cases


def grade(cases: list[dict], rows: list[dict]) -> dict:
    """A quick count per lane. scripts/guidance_compare.py gives the full metrics."""
    lanes = defaultdict(list)
    for case, row in zip(cases, rows, strict=True):
        lanes[case["lane"]].append((case, row))
    summary = {}
    for lane, items in sorted(lanes.items()):
        picked = [row["picks"] for _, row in items]
        entry = dict(
            prompts=len(items),
            picks_per_prompt=round(sum(map(len, picked)) / len(items), 2),
            failed_stages=sum(row["degraded"] for _, row in items),
            asked_to_choose=sum(row["decision"] == "choose" for _, row in items),
        )
        if lane.startswith("complex"):
            shares = [
                len(set(row["picks"]) & set(case["expected"])) / len(case["expected"])
                for case, row in items
            ]
            entry["found_share"] = round(sum(shares) / len(items), 3)
            entry["all_found"] = sum(share == 1 for share in shares)
        elif lane.startswith("unrelated"):
            entry["false_picks"] = sum(bool(row["picks"]) for _, row in items)
        elif lane.startswith("no_signal"):
            family = [
                len(set(row["picks"]) & set(case["family"])) / len(case["family"])
                for case, row in items
            ]
            entry["family_share"] = round(sum(family) / len(items), 3)
            entry["none_from_family"] = sum(share == 0 for share in family)
        else:
            right = sum(case["expected"][0] in row["picks"] for case, row in items)
            first = sum(row["picks"][:1] == case["expected"] for case, row in items)
            sibling = sum(
                bool(row["picks"])
                and row["picks"][0] != case["expected"][0]
                and row["picks"][0] in case["family"]
                for case, row in items
            )
            total = sum(map(len, picked))
            entry.update(
                recall=right,
                first_pick=first,
                first_pick_sibling=sibling,
                precision=round(right / total, 3) if total else 0.0,
            )
        summary[lane] = entry
    return summary


def arm_problem(
    arm: str,
    model_url,
    local_url,
    selector_url,
    api_key_env="RELAYMIND_EXTERNAL_API_KEY",
    model="unset",
) -> str | None:
    """Why an arm cannot run as named, because a report labeled with it would mislead."""
    stages = ARMS[arm]
    if stages.get("primary") == "http" and not selector_url:
        return f"--arm {arm} needs --selector-url"
    if "external" in (stages.get("keywords"), stages.get("picker")):
        if not model_url:
            return f"--arm {arm} needs --model-base-url"
        if not model:  # The provider would refuse every call, and each case fall back.
            return f"--arm {arm} needs --model"
        # Without the key every model call fails, and each case quietly falls back to word match.
        if not os.environ.get(api_key_env):
            return f"--arm {arm} needs {api_key_env} in the environment"
    if stages.get("keywords") == "local" and not local_url:
        return f"--arm {arm} needs --local-url"
    return None


def parse_options(values: list[str]) -> dict:
    """key=value options, each value read as JSON when it is, under the gateway's own rules."""
    options = {}
    for value in values:
        key, _, item = value.partition("=")
        try:
            options[key] = json.loads(item)
        except ValueError:
            options[key] = item
    return check_request_options(options)


async def run(args) -> dict:
    assets, cases = load_cases(args.split, Path(args.data))
    if args.background:
        assets = [*assets, *json.loads(Path(args.background).read_text("utf-8"))]
    if args.limit:
        cases = cases[: args.limit]
    arm = ARMS[args.arm]
    selector = None
    if arm.get("primary") == "http":
        selector = HttpAssetSelector(
            name=args.selector_name, version=args.selector_version, url=args.selector_url
        )
        arm = dict(
            arm, name=args.selector_name, version=args.selector_version, url=args.selector_url
        )
    defaults = GuidanceSettings()
    settings = replace(
        defaults,
        enabled=True,
        total_ms=args.total_ms or defaults.total_ms,
        shortlist=args.shortlist or defaults.shortlist,
        **arm,
    )
    tokens = Counter()

    def counted(chat):
        async def wrapped(messages, max_tokens):
            text, usage = await chat(messages, max_tokens)
            for key in ("input_tokens", "output_tokens"):
                tokens[key] += (usage or {}).get(key) or 0
            tokens["calls"] += 1
            return text, usage

        return wrapped

    external = None
    if args.model_base_url:
        external = counted(
            chat_client(
                url=f"{args.model_base_url.rstrip('/')}/chat/completions",
                model=args.model,
                api_key_env=args.api_key_env,
                extra=parse_options(args.request_option),
            )
        )
    local = local_keyword_client(args.local_url) if args.local_url else None
    clients = dict(local=local, external=external)
    keywords = clients.get(arm.get("keywords", "none"))
    picker = clients.get(arm.get("picker", "none"))
    gate = asyncio.Semaphore(args.workers)

    async def one(case):
        async with gate:
            started = time.monotonic()
            request = dict(schema_version=1, task_text=case["text"], assets=assets)
            if case.get("recent") and not args.no_recent:
                request["recent_text"] = case["recent"]
            result = await run_guidance(
                parse_guidance_request(request),
                settings,
                keywords=keywords,
                picker=picker,
                selector=selector,
            )
            return dict(
                case=case["case"],
                lane=case["lane"],
                text=case["text"],
                expected=case["expected"],
                picks=[pick["name"] for pick in result["picks"]],
                decision=result["decision"],
                degraded=result["degraded"],
                mechanism=result["mechanism"],
                stages={stage["stage"]: stage["outcome"] for stage in result["stages"]},
                ms=round((time.monotonic() - started) * 1000),
            )

    rows = await asyncio.gather(*(one(case) for case in cases))
    return dict(
        kind="guidance_similar",
        split=args.split,
        arm=args.arm,
        offered=len(assets),
        # Not every lane is held out: both splits share these prompts.
        shared_across_splits=dict(
            unrelated="the same prompts in both splits",
            follow_up="the same short phrases; the earlier request is the split's own",
        ),
        tokens=dict(tokens),
        summary=grade(cases, rows),
        rows=rows,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", required=True, choices=("development", "holdout"))
    parser.add_argument("--arm", required=True, choices=sorted(ARMS))
    parser.add_argument("--output", required=True)
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--background", help="JSON list of extra {name,kind,description} assets.")
    parser.add_argument("--model-base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key-env", default="RELAYMIND_EXTERNAL_API_KEY")
    parser.add_argument("--request-option", action="append", default=[])
    parser.add_argument("--local-url")
    parser.add_argument("--selector-url", help="For --arm third_party: your selector's /select.")
    parser.add_argument("--selector-name", default="third_party")
    parser.add_argument("--selector-version", default="1")
    parser.add_argument("--total-ms", type=int)
    parser.add_argument("--shortlist", type=int)
    parser.add_argument(
        "--no-recent", action="store_true", help="Send the prompt alone, without earlier text."
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if not os.path.exists(args.data):
        parser.error(f"no data file at {args.data}")
    problem = arm_problem(
        args.arm,
        args.model_base_url,
        args.local_url,
        args.selector_url,
        args.api_key_env,
        model=args.model,
    )
    try:
        parse_options(args.request_option)
    except ValueError as error:
        parser.error(f"--request-option: {error}")
    if problem:
        parser.error(problem)
    report = asyncio.run(run(args))
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps(dict(summary=report["summary"], tokens=report["tokens"]), indent=1))


if __name__ == "__main__":
    main()
