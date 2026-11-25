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
        language=compare.language_of(lane),
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


def test_an_early_selector_answer_is_counted_apart_and_is_not_a_failure():
    """Spec 0.73 section 7.7: a mechanism ending in :early is not the selector's full ranking."""
    report = dict(
        kind="guidance_similar",
        tokens={},
        rows=[
            dict(
                case=str(i),
                lane="en",
                text="t",
                expected=["x"],
                picks=["x"],
                decision="picks",
                degraded=False,
                ms=3,
                mechanism=mechanism,
            )
            for i, mechanism in enumerate(["router.hybrid-bm25:early", "router.hybrid-bm25"])
        ],
    )
    rows, _ = compare.read_report(report)
    assert [r["early"] for r in rows] == [True, False]
    m = compare.metrics(rows)
    assert m["early"] == 1 and m["degraded"] == 0
    assert "| Early |" in compare.table({"arm": {"metrics": m}})


def test_an_early_platform_answer_is_counted_apart_and_is_not_a_fallback():
    early = [dict(outcome="ranked", mechanism="router.hybrid-bm25:early")]
    rows, _ = compare.read_report(_platform([_platform_row("c", ["x"], attempts=early)]))
    assert rows[0]["early"] is True and rows[0]["degraded"] is False
    assert compare.metrics(rows)["early"] == 1


def test_rows_without_a_mechanism_have_no_early_answer():
    assert compare.metrics(ROWS)["early"] == 0


def test_a_partial_selector_answer_is_counted_apart_from_early_and_is_not_a_failure():
    """Spec 0.75 section 7.7: a mechanism ending in :partial ranked an offer the selector had only
    partly indexed, so it is not the selector's full ranking either."""
    row = dict(lane="en", text="t", expected=["x"], picks=["x"], decision="picks", degraded=False)
    mechanisms = ["router:partial", "router:early", "router"]
    report = dict(
        kind="guidance_similar",
        tokens={},
        rows=[dict(row, case=str(i), ms=3, mechanism=m) for i, m in enumerate(mechanisms)],
    )
    rows, _ = compare.read_report(report)
    assert [r["partial"] for r in rows] == [True, False, False]
    m = compare.metrics(rows)
    assert m["partial"] == 1 and m["early"] == 1 and m["degraded"] == 0
    assert "| Partial |" in compare.table({"arm": {"metrics": m}})
    platform = [dict(outcome="ranked", mechanism="router:partial")]
    rows, _ = compare.read_report(_platform([_platform_row("c", ["x"], attempts=platform)]))
    assert rows[0]["partial"] is True and rows[0]["degraded"] is False


# Spec 0.74 section 7.7: macro precision and macro recall are the main metrics (RM45, RM47).


def test_macro_precision_and_recall_count_every_pick_and_score_no_pick_as_zero():
    m = compare.metrics(ROWS)
    # Positive prompts only: a 1/1, b 1/4, c 0/1, d nothing picked, so 0.
    assert m["answer_prompts"] == 4
    assert m["macro_precision"] == pytest.approx((1 + 1 / 4 + 0 + 0) / 4)
    assert m["macro_recall"] == pytest.approx((1 + 1 + 0 + 0) / 4)
    # Micro: 2 right picks of 6 picks; 2 of 4 needed assets found.
    assert m["micro_precision"] == pytest.approx(2 / 6)
    assert m["micro_recall"] == pytest.approx(2 / 4)


def test_one_acceptable_asset_is_enough_for_a_one_skill_prompt():
    """Acceptable assets of a one-skill prompt are alternatives: any one answers it."""
    m = compare.metrics([row("a", "en", "positive", ["x", "x2"], ["x", "x2", "w"])])
    assert m["macro_precision"] == pytest.approx(2 / 3)
    assert m["macro_recall"] == 1.0
    assert m["micro_recall"] == 1.0


def test_a_workflow_needs_its_whole_set_for_full_recall():
    rows = [
        row("a", "complex_en", "workflow", ["p", "q", "r"], ["p", "q", "x", "y"]),
        row("b", "en", "positive", ["x"], ["x"]),
    ]
    m = compare.metrics(rows)
    assert m["answer_prompts"] == 2
    assert m["macro_precision"] == pytest.approx((2 / 4 + 1) / 2)
    assert m["macro_recall"] == pytest.approx((2 / 3 + 1) / 2)
    assert m["micro_precision"] == pytest.approx(3 / 5)
    assert m["micro_recall"] == pytest.approx(3 / 4)


def test_unrelated_and_family_prompts_stay_out_of_macro_precision_and_recall():
    """There, picking nothing or asking is the right answer; they keep their own metrics."""
    rows = [
        row("a", "en", "positive", ["x"], ["x"]),
        row("b", "unrelated_en", "negative", [], ["z"]),
        row("c", "no_signal_en", "ambiguous", ["p", "q"], ["z"]),
    ]
    m = compare.metrics(rows)
    assert (m["answer_prompts"], m["macro_precision"], m["macro_recall"]) == (1, 1.0, 1.0)


def test_no_prompt_with_an_answer_leaves_macro_precision_and_recall_unknown():
    m = compare.metrics([row("a", "unrelated_en", "negative", [], [])])
    assert m["macro_precision"] is None and m["macro_recall"] is None
    assert m["micro_precision"] is None and m["micro_recall"] is None


@pytest.mark.parametrize(
    "lane, language",
    [
        ("en", "en"),
        ("zh", "zh"),
        ("complex_zh", "zh"),
        ("follow_up_en", "en"),
        ("zh_to_en", "zh"),
        ("en_to_en", "en"),
        ("tie", None),
    ],
)
def test_the_language_of_a_lane_is_its_prompt_language(lane, language):
    assert compare.language_of(lane) == language


def test_macro_precision_and_recall_are_split_by_language():
    by = compare.by_language(ROWS)
    assert set(by) == {"en", "zh"}
    assert by["en"]["macro_precision"] == pytest.approx((1 + 1 / 4) / 2)
    assert by["en"]["macro_recall"] == 1.0
    assert by["zh"]["macro_precision"] == 0.0
    assert by["zh"]["macro_recall"] == 0.0


def test_the_paired_test_is_also_run_on_precision_and_recall_per_prompt():
    base = [
        row("a", "en", "positive", ["x"], ["x", "w", "v"]),
        row("b", "en", "positive", ["x"], []),
    ]
    arm = [
        row("a", "en", "positive", ["x"], ["x"]),  # same recall, better precision
        row("b", "en", "positive", ["x"], ["w"]),  # recall still 0, precision still 0
    ]
    assert compare.paired(base, arm, score="precision")["wins"] == 1
    assert compare.paired(base, arm, score="recall")["same"] == 2
    # The default stays the first pick, so earlier reports read the same.
    assert compare.paired(base, arm) == compare.paired(base, arm, score="first_pick")


def test_the_table_leads_with_macro_precision_and_recall():
    m = compare.metrics(ROWS)
    header = compare.table({"arm": {"metrics": m}}).splitlines()[0]
    assert header.startswith("| Arm | Macro P | Macro R | Micro P | Micro R |")


def test_the_command_prints_macro_precision_and_recall_by_language(tmp_path, capsys):
    rows = [
        dict(
            case=c,
            lane=lane,
            text="t",
            expected=["x"],
            picks=picks,
            decision="picks",
            degraded=False,
            ms=3,
        )
        for c, lane, picks in [("1", "en", ["x"]), ("2", "zh", ["w"])]
    ]
    path, out = tmp_path / "r.json", tmp_path / "o.json"
    path.write_text(json.dumps(dict(kind="guidance_similar", tokens={}, rows=rows)))
    compare.main(["--baseline", f"floor={path}", "--arm", f"mine={path}", "--output", str(out)])
    shown = capsys.readouterr().out
    assert "By language" in shown and "| floor | en |" in shown and "| mine | zh |" in shown
    saved = json.loads(out.read_text())["arms"]["mine"]
    assert saved["by_language"]["en"]["macro_precision"] == 1.0
    assert saved["paired_precision"]["same"] == 2 and saved["paired_recall"]["same"] == 2


def test_a_report_row_carries_its_prompt_language():
    """read_report sets the language once: a source case's own, else the lane's."""
    similar = dict(
        kind="guidance_similar",
        rows=[
            dict(
                case="1",
                lane="complex_zh",
                expected=["x"],
                picks=[],
                decision="no_match",
                degraded=False,
                ms=1,
            )
        ],
    )
    assert compare.read_report(similar)[0][0]["language"] == "zh"
    source = _platform([_platform_row("c", ["x"])])
    source["input_document"]["cases"][0]["query_language"] = "zh"
    assert compare.read_report(source)[0][0]["language"] == "zh"
    assert compare.read_report(_platform([_platform_row("c", ["x"])]))[0][0]["language"] == "en"


def test_a_mixed_language_source_case_is_its_own_language_row():
    """Spec 0.74: a source case's query_language decides, so code-switched prompts are not lost."""
    report = _platform([_platform_row("c", ["x"])], cases=("c",))
    report["input_document"]["cases"][0].update(lane="code_switch", query_language="mixed")
    rows, _ = compare.read_report(report)
    assert compare.language_of("code_switch") is None
    assert set(compare.by_language(rows)) == {"mixed"}
