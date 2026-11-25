#!/usr/bin/env python3
"""Compare selectors with a baseline on precision, recall and more (RM16, RM40, RM45).

Reads reports of the same prompts, from guidance_similar_benchmark.py or from
source_asset_benchmark.py run. Each arm can be any selector: the built-in stages or your own.

  python scripts/guidance_compare.py --baseline word=word.json --arm mine=mine.json

The main metrics (spec 0.74 section 7.7), over prompts with an answer to find: one-skill prompts
and big workflows. Every pick counts, and a prompt with no pick scores 0.
  macro P      the mean over prompts of the share of picks that are right
  macro R      the mean over prompts of how much was found: for a one-skill prompt, 1 when any
               acceptable asset was picked; for a workflow, the share of its needed set
  micro P, R   the same over all picks and all needed assets together
  P, R wins    prompts where only one side had better precision or recall, with a sign test
They are also shown per prompt language. Unrelated prompts and prompts that fit a whole family
stay out of them: there, picking nothing or asking is the right answer.

Secondary, over one-skill prompts:
  first pick   the first pick is right
  MRR          mean of 1 / the rank of the first right pick, 0 when none
  P@3          of the first 3 picks, the share that are right; prompts with no pick are left out
  R@3, R@8     the first 3 or 8 picks hold a right answer
  F1@3         2 * P@3 * R@3 / (P@3 + R@3)
  picks        picks per prompt, as delivered

Over the other prompts:
  false picks  an unrelated prompt got any pick; lower is better
  asked        a prompt that fits several assets got the answer "choose"
  offered      the share of those several assets that were offered

Over big workflows, where a prompt needs a whole set of assets:
  set found    the share of the needed set that was picked
  all found    the whole needed set was picked
  set exact    of the picks, the share that are needed
  set picks    picks per prompt

Over all prompts:
  failed       the selector did not answer, so a fallback did or nothing did
  early        the selector ranked early, from the features it had indexed so far (spec 0.73),
               not with its full ranking; such answers count as answers, not failures
  partial      the selector ranked an offer it had only partly indexed (spec 0.75), so not with
               its full ranking either; counted like early, in its own column

Development evidence unless the data has independent labels. The formal gates are in the benchmarks'
own compare commands; this tool adds metrics and sets no gate.
"""

import argparse
import json
import math
import statistics
import sys
from pathlib import Path


def _lane_kind(lane: str) -> str:
    if lane.startswith("no_signal"):
        return "ambiguous"
    if lane.startswith("complex"):
        return "workflow"
    if lane.startswith("unrelated"):
        return "negative"
    return "positive"


def read_report(report: dict) -> tuple[list[dict], int | None]:
    """(rows, input tokens or None) of a report of either benchmark."""
    if report.get("kind") == "guidance_similar":
        rows = [
            dict(
                case=r["case"],
                lane=r["lane"],
                kind=_lane_kind(r["lane"]),
                language=language_of(r["lane"]),
                acceptable=r["expected"],
                picks=r["picks"],
                decision=r["decision"],
                degraded=r["degraded"],
                early=_early(r.get("mechanism")),
                partial=_partial(r.get("mechanism")),
                ms=r["ms"],
            )
            for r in report["rows"]
        ]
        return rows, report.get("tokens", {}).get("input_tokens")
    rows = report.get("rows")
    if "input_document" in report and isinstance(rows, list) and (not rows or "slot" in rows[0]):
        cases = report["input_document"]["cases"]
        lanes = {c["id"]: c["lane"] for c in cases}
        languages = {c["id"]: c.get("query_language") or language_of(c["lane"]) for c in cases}
        return [
            dict(
                case=(
                    f"{r['slot']['case_id']}#{r['slot']['repetition']}"
                    if r["slot"].get("repetition") is not None
                    else r["slot"]["case_id"]
                ),
                case_id=r["slot"]["case_id"],
                lane=lanes[r["slot"]["case_id"]],
                language=languages[r["slot"]["case_id"]],
                kind=r["grade"]["class"],
                acceptable=r["acceptable_asset_ids"],
                picks=[c["asset_id"] for c in r["evaluated_candidates"]],
                # Selection v2 has no "choose", so whether it asked is unknown, not "no".
                decision=None,
                degraded=_fell_back(r),
                early=_early(_first_mechanism(r)),
                partial=_partial(_first_mechanism(r)),
                ms=r["observed_ms"],
            )
            for r in report["rows"]
        ], None
    raise ValueError("this file is not a report of a known benchmark")


def _fell_back(row: dict) -> bool:
    """A platform row whose own selector did not answer, even if a fallback did.

    The gateway's fallback can still make the row "ready", so the first attempt decides.
    """
    attempts = row.get("attempts") or []
    if row["status"] not in ("ready", "ranked", "no_match") or len(attempts) > 1:
        return True
    first = attempts[0] if attempts else {}
    # A label for an early or partial answer comes after `_degraded` (spec 0.73 and 0.75).
    mechanism = str(first.get("mechanism", "")).removesuffix(":partial").removesuffix(":early")
    return first.get("outcome", "ranked") not in ("ranked", "no_match") or mechanism.endswith(
        "_degraded"
    )


def _first_mechanism(row: dict) -> object:
    """The mechanism of a platform row's first attempt: the configured selector's own answer."""
    return ((row.get("attempts") or [{}])[0]).get("mechanism")


def _early(mechanism: object) -> bool:
    """A selector answer ranked from an earlier stage while later features index (spec 0.73)."""
    return str(mechanism or "").endswith(":early")


def _partial(mechanism: object) -> bool:
    """A selector answer over an offer it had only partly indexed (spec 0.75)."""
    return str(mechanism or "").endswith(":partial")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _right_picks(row: dict) -> int:
    return len(set(row["picks"]) & set(row["acceptable"]))


def _found_share(row: dict) -> float:
    """How much of a workflow's needed set was picked."""
    return _right_picks(row) / len(row["acceptable"])


def _first_right(row: dict) -> int | None:
    right = set(row["acceptable"])
    return next((i for i, name in enumerate(row["picks"]) if name in right), None)


def _precision(row: dict, k: int) -> float:
    top = row["picks"][:k]
    return len(set(top) & set(row["acceptable"])) / len(top)


def language_of(lane: str) -> str | None:
    """The prompt language, "en" or "zh", that a lane names, or None.

    "zh" and "complex_zh" give "zh"; a source lane such as "zh_hans_to_en" gives its source side.
    """
    parts = lane.split("_")
    language = parts[0] if "to" in parts else parts[-1]
    return language if language in ("en", "zh") else None


def _is_answer(row: dict) -> bool:
    """A prompt with an answer to find: a one-skill prompt or a big workflow."""
    return bool(row["acceptable"]) and row["kind"] in ("positive", "workflow")


def _answer_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if _is_answer(r)]


def _found_and_needed(row: dict) -> tuple[int, int]:
    """Needed assets found and needed: any one acceptable asset answers a one-skill prompt."""
    right = _right_picks(row)
    if row["kind"] == "positive":
        return min(right, 1), 1
    return right, len(set(row["acceptable"]))


def _case_precision(row: dict) -> float:
    picks = set(row["picks"])
    return _right_picks(row) / len(picks) if picks else 0.0


def _case_recall(row: dict) -> float:
    found, needed = _found_and_needed(row)
    return found / needed


def set_metrics(rows: list[dict]) -> dict:
    """Macro and micro precision and recall of every pick (spec 0.74 section 7.7)."""
    answer = _answer_rows(rows)
    if not answer:
        return dict(
            answer_prompts=0,
            macro_precision=None,
            macro_recall=None,
            micro_precision=None,
            micro_recall=None,
        )
    picks = sum(len(set(r["picks"])) for r in answer)
    found, needed = (sum(pair) for pair in zip(*map(_found_and_needed, answer), strict=True))
    return dict(
        answer_prompts=len(answer),
        macro_precision=_mean([_case_precision(r) for r in answer]),
        macro_recall=_mean([_case_recall(r) for r in answer]),
        micro_precision=sum(_right_picks(r) for r in answer) / picks if picks else 0.0,
        micro_recall=found / needed,
    )


def by_language(rows: list[dict]) -> dict:
    """set_metrics per prompt language, for the languages the answer prompts carry."""
    groups: dict[str, list[dict]] = {}
    for row in _answer_rows(rows):
        if row.get("language"):
            groups.setdefault(row["language"], []).append(row)
    return {language: set_metrics(groups[language]) for language in sorted(groups)}


def metrics(rows: list[dict]) -> dict:
    positive = [r for r in rows if r["kind"] == "positive"]
    negative = [r for r in rows if r["kind"] == "negative"]
    ambiguous = [r for r in rows if r["kind"] == "ambiguous"]
    workflow = [r for r in rows if r["kind"] == "workflow" and r["acceptable"]]
    workflow_picked = [r for r in workflow if r["picks"]]
    ranks = [_first_right(r) for r in positive]
    picked = [r for r in positive if r["picks"]]
    precision = (_mean([_precision(r, 3) for r in picked]) or 0.0) if positive else None
    recall = (_mean([rank is not None and rank < 3 for rank in ranks]) or 0.0) if positive else None
    f1 = None
    if positive:
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    times = sorted(r["ms"] for r in rows if r["ms"] is not None)
    return dict(
        prompts=len(rows),
        **set_metrics(rows),
        positive_prompts=len(positive),
        first_pick=_mean([rank == 0 for rank in ranks]),
        mrr=_mean([0.0 if rank is None else 1 / (rank + 1) for rank in ranks]),
        precision_at_3=precision,
        recall_at_3=recall,
        f1_at_3=f1,
        recall_at_8=_mean([rank is not None and rank < 8 for rank in ranks]),
        picks_per_prompt=_mean([len(r["picks"]) for r in positive]),
        unrelated_prompts=len(negative),
        unrelated_false_picks=_mean([bool(r["picks"]) for r in negative]),
        undecided_prompts=len(ambiguous),
        undecided_asked=_mean(
            [r["decision"] == "choose" for r in ambiguous if r["decision"] is not None]
        ),
        undecided_share_offered=_mean([_found_share(r) for r in ambiguous if r["acceptable"]]),
        workflow_prompts=len(workflow),
        workflow_found=_mean([_found_share(r) for r in workflow]),
        workflow_all_found=_mean([_found_share(r) == 1 for r in workflow]),
        workflow_precision=_mean([_right_picks(r) / len(r["picks"]) for r in workflow_picked]),
        workflow_picks=_mean([len(r["picks"]) for r in workflow]),
        degraded=sum(bool(r["degraded"]) for r in rows),
        early=sum(bool(r.get("early")) for r in rows),
        partial=sum(bool(r.get("partial")) for r in rows),
        p50_ms=statistics.median(times) if times else None,
        p95_ms=times[math.ceil(len(times) * 0.95) - 1] if times else None,
    )


def _first_pick_score(row: dict) -> float | None:
    """A right first pick, or for a workflow the share of its set found; None if not counted."""
    if row["kind"] == "workflow" and row["acceptable"]:
        return _found_share(row)
    if row["kind"] == "positive":
        return float(_first_right(row) == 0)
    return None


# Per-prompt scores for the sign test; None leaves a prompt out (spec 0.74).
_SCORES = {
    "first_pick": _first_pick_score,
    "precision": lambda row: _case_precision(row) if _is_answer(row) else None,
    "recall": lambda row: _case_recall(row) if _is_answer(row) else None,
}


def paired(base: list[dict], arm: list[dict], score: str = "first_pick") -> dict:
    """Prompts where one side did better, with an exact two-sided sign test.

    Better is, by `score`: "first_pick", a right first pick, or for a workflow more of the needed
    set; "precision" or "recall", that prompt's precision or recall of every pick (spec 0.74).
    """
    if score not in _SCORES:
        raise ValueError(f"unknown score {score}")
    scored = _SCORES[score]
    before = {r["case"]: r for r in base}
    after = {r["case"]: r for r in arm}
    if len(before) != len(base) or len(after) != len(arm):
        raise ValueError("a case repeats, so these reports cannot be paired")
    if set(before) != set(after):
        raise ValueError("these reports are not the same prompts")
    # Repetitions of one prompt are one pair: each side's score is its mean over them, so
    # running a prompt more often cannot make a difference look more certain.
    scores: dict[str, list[list[float]]] = {}
    for case, left in before.items():
        right = after[case]
        if set(left["acceptable"]) != set(right["acceptable"]):
            raise ValueError("these reports are not the same expected answers")
        was, now = scored(left), scored(right)
        if was is None or now is None:
            continue
        scores.setdefault(left.get("case_id", case), []).append([was, now])
    wins = losses = same = 0
    for pairs in scores.values():
        was = sum(p[0] for p in pairs) / len(pairs)
        now = sum(p[1] for p in pairs) / len(pairs)
        wins += now > was
        losses += now < was
        same += now == was
    changed = wins + losses
    tail = sum(math.comb(changed, i) for i in range(min(wins, losses) + 1)) / 2**changed
    return dict(wins=wins, losses=losses, same=same, sign_test_p=min(1.0, 2 * tail))


def _named(value: str) -> tuple[str, Path]:
    name, separator, path = value.partition("=")
    if not (name and separator and path):
        raise argparse.ArgumentTypeError("write it as name=report.json")
    return name, Path(path)


def _cell(value: float | None, percent: bool = True) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.0f}%" if percent else f"{value:.2f}"


def _pr_cells(m: dict) -> str:
    """Macro P, macro R, micro P and micro R as table cells."""
    keys = ("macro_precision", "macro_recall", "micro_precision", "micro_recall")
    return " | ".join(_cell(m[key]) for key in keys)


def _versus(pair: dict | None) -> str:
    return f"{pair['wins']}/{pair['losses']} (p={pair['sign_test_p']:.3g})" if pair else "baseline"


def table(arms: dict) -> str:
    lines = [
        "| Arm | Macro P | Macro R | Micro P | Micro R | P wins/losses (p) | R wins/losses (p) | "
        "First pick | MRR | P@3 | R@3 | F1@3 | R@8 | Picks | False picks | Asked | "
        "Offered | Set found | All found | Set exact | Set picks | Failed | Early | Partial | "
        "p50 ms | "
        "First pick wins/losses (p) |",
        "|---|---:|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
        "---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name, arm in arms.items():
        m = arm["metrics"]
        p50 = "-" if m["p50_ms"] is None else f"{m['p50_ms']:.0f}"
        lines.append(
            f"| {name} | {_pr_cells(m)} | "
            f"{_versus(arm.get('paired_precision'))} | {_versus(arm.get('paired_recall'))} | "
            f"{_cell(m['first_pick'])} | {_cell(m['mrr'], False)} | "
            f"{_cell(m['precision_at_3'])} | {_cell(m['recall_at_3'])} | "
            f"{_cell(m['f1_at_3'], False)} | {_cell(m['recall_at_8'])} | "
            f"{_cell(m['picks_per_prompt'], False)} | {_cell(m['unrelated_false_picks'])} | "
            f"{_cell(m['undecided_asked'])} | {_cell(m['undecided_share_offered'])} | "
            f"{_cell(m['workflow_found'])} | {_cell(m['workflow_all_found'])} | "
            f"{_cell(m['workflow_precision'])} | {_cell(m['workflow_picks'], False)} | "
            f"{m['degraded']} | {m['early']} | {m['partial']} | {p50} | "
            f"{_versus(arm.get('paired_with_baseline'))} |"
        )
    return "\n".join(lines)


def language_table(arms: dict) -> str:
    """Macro and micro precision and recall of each arm per prompt language."""
    lines = [
        "| Arm | Language | Prompts | Macro P | Macro R | Micro P | Micro R |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for name, arm in arms.items():
        for language, m in arm["by_language"].items():
            lines.append(f"| {name} | {language} | {m['answer_prompts']} | {_pr_cells(m)} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=_named, required=True, metavar="NAME=REPORT")
    parser.add_argument("--arm", type=_named, action="append", default=[], metavar="NAME=REPORT")
    parser.add_argument("--by-lane", action="store_true", help="Also print one table per lane.")
    parser.add_argument("--output", type=Path, help="Write the numbers as JSON here.")
    args = parser.parse_args(argv)

    def load(path: Path) -> tuple[list[dict], int | None]:
        return read_report(json.loads(path.read_text("utf-8")))

    base_name, base_path = args.baseline
    base_rows, base_tokens = load(base_path)
    loaded = {base_name: (base_rows, base_tokens)}
    for name, path in args.arm:
        if name in loaded:  # The later report would replace the earlier one under one label.
            parser.error(f"two reports are named {name}")
        loaded[name] = load(path)

    def build(select, languages: bool = False) -> dict:
        arms = {}
        base = select(base_rows)
        for name, (rows, tokens) in loaded.items():
            chosen = select(rows)
            arms[name] = dict(metrics=metrics(chosen), input_tokens=tokens)
            if languages:
                arms[name]["by_language"] = by_language(chosen)
            if name != base_name:
                arms[name]["paired_precision"] = paired(base, chosen, score="precision")
                arms[name]["paired_recall"] = paired(base, chosen, score="recall")
                arms[name]["paired_with_baseline"] = paired(base, chosen)
        return arms

    arms = build(lambda rows: rows, languages=True)
    print(table(arms))
    if any(arm["by_language"] for arm in arms.values()):
        print(f"\nBy language\n{language_table(arms)}")
    lanes = {}
    if args.by_lane:
        for lane in sorted({r["lane"] for r in base_rows}):
            lanes[lane] = build(lambda rows, lane=lane: [r for r in rows if r["lane"] == lane])
            print(f"\n{lane}\n{table(lanes[lane])}")
    if args.output:
        args.output.write_text(json.dumps(dict(arms=arms, lanes=lanes), indent=1), "utf-8")


if __name__ == "__main__":
    sys.exit(main())
