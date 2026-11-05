"""The metrics a selector is compared on (RM16, RM40, RM45; spec 0.69 section 7.7)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gc", ROOT / "scripts/guidance_compare.py")
compare = importlib.util.module_from_spec(spec)
sys.modules["gc_module"] = compare
spec.loader.exec_module(compare)


def row(case, lane, kind, right, picks, **extra):
    return dict(
        case=case,
        lane=lane,
        kind=kind,
        acceptable=right,
        picks=picks,
        decision=extra.get("decision", "picks" if picks else "no_match"),
        degraded=extra.get("degraded", False),
        ms=extra.get("ms", 10),
    )


ROWS = [
    row("a", "en", "positive", ["x"], ["x"]),
    row("b", "en", "positive", ["y"], ["z", "y", "q", "r"]),
    row("c", "zh", "positive", ["k"], ["m"]),
    row("d", "zh", "positive", ["n"], []),
    row("e", "none", "negative", [], []),
    row("f", "none", "negative", [], ["z"]),
    row("g", "tie", "ambiguous", ["p", "q"], ["p", "q"], decision="choose"),
    row("h", "tie", "ambiguous", ["s", "t"], ["s"]),
]


def test_each_metric_on_a_small_hand_counted_case():
    m = compare.metrics(ROWS)
    assert m["positive_prompts"] == 4
    assert m["first_pick"] == 0.25  # a only
    assert m["mrr"] == pytest.approx((1 + 1 / 2 + 0 + 0) / 4)
    assert m["recall_at_3"] == 0.5  # a and b
    # Means over prompts that picked: a 1/1, b 1/3 inside the top 3, c 0/1.
    assert m["precision_at_3"] == pytest.approx((1 + 1 / 3 + 0) / 3)
    p, r = m["precision_at_3"], m["recall_at_3"]
    assert m["f1_at_3"] == pytest.approx(2 * p * r / (p + r))
    assert m["recall_at_8"] == 0.5
    assert m["picks_per_prompt"] == pytest.approx((1 + 4 + 1 + 0) / 4)


def test_unrelated_and_undecided_prompts_are_reported_apart():
    m = compare.metrics(ROWS)
    assert m["unrelated_prompts"] == 2
    assert m["unrelated_false_picks"] == 0.5
    assert m["undecided_prompts"] == 2
    assert m["undecided_asked"] == 0.5
    assert m["undecided_share_offered"] == pytest.approx((1 + 0.5) / 2)


def test_p95_is_the_nearest_rank():
    rows = [row(str(i), "en", "positive", ["x"], ["x"], ms=i) for i in range(1, 21)]
    assert compare.metrics(rows)["p95_ms"] == 19


def test_no_prompts_of_a_kind_is_unknown_not_zero():
    m = compare.metrics([row("a", "en", "positive", ["x"], ["x"])])
    assert m["unrelated_false_picks"] is None
    assert m["undecided_asked"] is None


def test_nothing_picked_gives_zero_precision_and_f1_without_dividing_by_zero():
    m = compare.metrics([row("a", "en", "positive", ["x"], [])])
    assert m["precision_at_3"] == 0.0
    assert m["f1_at_3"] == 0.0


def test_the_paired_test_counts_wins_and_losses_on_the_same_prompts():
    base = [row(c, "en", "positive", ["x"], ["w"]) for c in "abcdef"]
    arm = [row(c, "en", "positive", ["x"], ["x"]) for c in "abcde"]
    arm.append(row("f", "en", "positive", ["x"], ["w"]))
    paired = compare.paired(base, arm)
    assert paired == dict(wins=5, losses=0, same=1, sign_test_p=0.0625)


def test_a_loss_counts_and_a_tie_of_wins_and_losses_is_p_one():
    base = [row("a", "en", "positive", ["x"], ["x"]), row("b", "en", "positive", ["x"], ["w"])]
    arm = [row("a", "en", "positive", ["x"], ["w"]), row("b", "en", "positive", ["x"], ["x"])]
    assert compare.paired(base, arm) == dict(wins=1, losses=1, same=0, sign_test_p=1.0)


def test_arms_over_different_prompts_are_refused():
    base = [row("a", "en", "positive", ["x"], ["x"])]
    arm = [row("b", "en", "positive", ["x"], ["x"])]
    with pytest.raises(ValueError, match="not the same prompts"):
        compare.paired(base, arm)


def test_arms_with_different_expected_answers_are_refused():
    base = [row("a", "en", "positive", ["x"], ["x"])]
    arm = [row("a", "en", "positive", ["y"], ["x"])]
    with pytest.raises(ValueError, match="not the same expected"):
        compare.paired(base, arm)


def test_reads_a_similar_benchmark_report():
    report = dict(
        kind="guidance_similar",
        tokens=dict(input_tokens=5),
        rows=[
            dict(
                case="1",
                lane="en",
                text="t",
                expected=["x"],
                picks=["x"],
                decision="picks",
                degraded=False,
                ms=3,
            ),
            dict(
                case="2",
                lane="no_signal_zh",
                text="u",
                expected=["x", "y"],
                picks=["x"],
                decision="picks",
                degraded=False,
                ms=3,
            ),
            dict(
                case="3",
                lane="unrelated_en",
                text="v",
                expected=[],
                picks=[],
                decision="no_match",
                degraded=False,
                ms=3,
            ),
        ],
    )
    rows, tokens = compare.read_report(report)
    assert [r["kind"] for r in rows] == ["positive", "ambiguous", "negative"]
    assert tokens == 5


def test_reads_a_source_benchmark_report():
    report = dict(
        input_document=dict(cases=[dict(id="c1", lane="en_to_en")]),
        rows=[
            dict(
                slot=dict(case_id="c1"),
                grade={"class": "positive"},
                acceptable_asset_ids=["x"],
                evaluated_candidates=[dict(asset_id="x")],
                evaluated_decision="confident",
                status="ranked",
                observed_ms=4.0,
            )
        ],
    )
    rows, _ = compare.read_report(report)
    assert rows[0]["picks"] == ["x"]
    assert rows[0]["case"] == "c1"
    assert rows[0]["lane"] == "en_to_en"
    assert rows[0]["ms"] == 4.0
    assert rows[0]["degraded"] is False


def test_an_unknown_report_is_refused():
    with pytest.raises(ValueError, match="report"):
        compare.read_report(dict(hello=1))


def test_the_command_prints_a_table_and_writes_json(tmp_path, capsys):
    rows = [
        dict(
            case="1",
            lane="en",
            text="t",
            expected=["x"],
            picks=["x"],
            decision="picks",
            degraded=False,
            ms=3,
        )
    ]
    report = dict(kind="guidance_similar", tokens={}, rows=rows)
    base, arm, out = tmp_path / "b.json", tmp_path / "a.json", tmp_path / "o.json"
    base.write_text(json.dumps(report))
    arm.write_text(json.dumps(report))
    compare.main(
        ["--baseline", f"floor={base}", "--arm", f"mine={arm}", "--output", str(out)],
    )
    shown = capsys.readouterr().out
    assert "floor" in shown and "mine" in shown and "F1@3" in shown
    assert json.loads(out.read_text())["arms"]["mine"]["paired_with_baseline"]["same"] == 1


def test_a_bad_arm_argument_stops_with_a_clear_message(tmp_path):
    with pytest.raises(SystemExit):
        compare.main(["--baseline", "no-equals-sign"])


def test_repeated_cases_are_refused_rather_than_silently_dropped():
    rows = [row("a", "en", "positive", ["x"], ["x"]), row("a", "en", "positive", ["x"], ["w"])]
    with pytest.raises(ValueError, match="repeat"):
        compare.paired(rows, rows)


def test_each_repetition_of_a_source_benchmark_case_is_its_own_pair():
    item = dict(
        grade={"class": "positive"},
        acceptable_asset_ids=["x"],
        evaluated_candidates=[],
        evaluated_decision="no_match",
        status="ready",
        observed_ms=1.0,
    )
    report = dict(
        input_document=dict(cases=[dict(id="c", lane="en_to_en")]),
        rows=[
            dict(item, slot=dict(case_id="c", repetition=0)),
            dict(item, slot=dict(case_id="c", repetition=1)),
        ],
    )
    rows, _ = compare.read_report(report)
    assert len({r["case"] for r in rows}) == 2


def test_a_workflow_prompt_is_measured_on_how_complete_and_how_exact_its_set_is():
    rows = [
        row("a", "complex_en", "workflow", ["x", "y", "z"], ["x", "y", "q"]),
        row("b", "complex_en", "workflow", ["x", "y"], ["x", "y"]),
        row("c", "complex_en", "workflow", ["x", "y"], []),
    ]
    m = compare.metrics(rows)
    assert m["workflow_prompts"] == 3
    assert m["workflow_found"] == pytest.approx((2 / 3 + 1 + 0) / 3)
    assert m["workflow_all_found"] == pytest.approx(1 / 3)
    assert m["workflow_precision"] == pytest.approx((2 / 3 + 1) / 2)
    assert m["workflow_picks"] == pytest.approx((3 + 2 + 0) / 3)
    assert m["positive_prompts"] == 0


def test_a_workflow_lane_is_read_as_a_workflow():
    report = dict(
        kind="guidance_similar",
        rows=[
            dict(
                case="1",
                lane="complex_zh",
                expected=["x", "y", "z"],
                picks=["x"],
                decision="picks",
                degraded=False,
                ms=1,
            )
        ],
    )
    rows, _ = compare.read_report(report)
    assert rows[0]["kind"] == "workflow"


def test_the_paired_test_counts_a_workflow_by_how_much_of_its_set_was_found():
    base = [row("a", "complex_en", "workflow", ["x", "y"], ["x"])]
    arm = [row("a", "complex_en", "workflow", ["x", "y"], ["x", "y"])]
    assert compare.paired(base, arm)["wins"] == 1


def test_an_undecided_prompt_with_no_expected_answers_does_not_crash():
    rows = [row("a", "no_signal_en", "ambiguous", [], ["x"])]
    assert compare.metrics(rows)["undecided_share_offered"] is None


def test_a_workflow_with_no_expected_answers_does_not_crash_the_paired_test():
    rows = [row("a", "complex_en", "workflow", [], ["x"])]
    assert compare.paired(rows, rows) == dict(wins=0, losses=0, same=0, sign_test_p=1.0)


def test_every_repetition_of_a_source_case_is_named_the_same_way():
    item = dict(
        grade={"class": "positive"},
        acceptable_asset_ids=["x"],
        evaluated_candidates=[],
        evaluated_decision="no_match",
        status="ready",
        observed_ms=1.0,
    )
    report = dict(
        input_document=dict(cases=[dict(id="c", lane="en_to_en")]),
        rows=[
            dict(item, slot=dict(case_id="c", repetition=0)),
            dict(item, slot=dict(case_id="c", repetition=1)),
        ],
    )
    rows, _ = compare.read_report(report)
    assert [r["case"] for r in rows] == ["c#0", "c#1"]


def test_an_empty_platform_report_is_read_as_no_rows():
    """Review of PR 285: zero rows is still a known report."""
    report = dict(rows=[], input_document=dict(cases=[]))
    assert compare.read_report(report) == ([], None)


def _platform(rows, cases=("c",)):
    return dict(input_document=dict(cases=[dict(id=c, lane="en_to_en") for c in cases]), rows=rows)


def _platform_row(case, picks, rep=None, attempts=None, decision="choice_required"):
    return dict(
        slot=dict(case_id=case, repetition=rep),
        grade={"class": "positive"},
        acceptable_asset_ids=["x"],
        evaluated_candidates=[dict(asset_id=p) for p in picks],
        evaluated_decision=decision,
        status="ready",
        observed_ms=1.0,
        attempts=attempts or [dict(outcome="ranked", mechanism="m")],
    )


@pytest.mark.parametrize(
    "attempts",
    [
        [dict(outcome="invalid", mechanism="m"), dict(outcome="ranked", mechanism="lexical")],
        [dict(outcome="ranked", mechanism="guidance_word_match_degraded")],
        [dict(outcome="timeout", mechanism="m")],
    ],
)
def test_a_platform_row_that_fell_back_counts_as_failed(attempts):
    rows, _ = compare.read_report(_platform([_platform_row("c", ["x"], attempts=attempts)]))
    assert rows[0]["degraded"] is True
    assert compare.metrics(rows)["degraded"] == 1


def test_a_platform_report_shows_no_asked_share():
    """Selection v2 has no choose, so the share is unknown, not 0%."""
    rows, _ = compare.read_report(_platform([_platform_row("c", ["x"])]))
    rows[0]["kind"] = "ambiguous"
    assert compare.metrics(rows)["undecided_asked"] is None


def test_repetitions_of_one_prompt_are_one_pair_in_the_sign_test():
    base, _ = compare.read_report(_platform([_platform_row("c", [], rep=i) for i in range(6)]))
    arm, _ = compare.read_report(_platform([_platform_row("c", ["x"], rep=i) for i in range(6)]))
    pair = compare.paired(base, arm)
    assert (pair["wins"], pair["losses"]) == (1, 0)


def test_two_arms_with_one_name_are_refused(tmp_path, capsys):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(dict(kind="guidance_similar", rows=[], tokens={})), "utf-8")
    with pytest.raises(SystemExit):
        compare.main(["--baseline", f"word={path}", "--arm", f"word={path}"])
    assert "word" in capsys.readouterr().err
