"""The data and harness for similar assets stay sound (RM16, RM45; spec 0.69 section 7.7)."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gsb", ROOT / "scripts/guidance_similar_benchmark.py")
harness = importlib.util.module_from_spec(spec)
sys.modules["gsb"] = harness
spec.loader.exec_module(harness)


@pytest.mark.parametrize(("split", "skills"), [("development", 32), ("holdout", 70)])
def test_each_split_has_every_kind_of_prompt(split, skills):
    assets, cases = harness.load_cases(split)
    assert len(assets) == skills
    assert len({a["name"] for a in assets}) == skills
    lanes = {c["lane"] for c in cases}
    assert {"en", "zh", "follow_up_en", "follow_up_zh", "no_signal_en", "no_signal_zh"} <= lanes
    assert {"unrelated_en", "unrelated_zh"} <= lanes
    assert len({c["case"] for c in cases}) == len(cases)


def test_the_splits_share_no_skill():
    development = {a["name"] for a in harness.load_cases("development")[0]}
    holdout = {a["name"] for a in harness.load_cases("holdout")[0]}
    assert not development & holdout


def test_a_follow_up_carries_the_earlier_request_and_names_no_skill():
    _, cases = harness.load_cases("development")
    follow_ups = [c for c in cases if c["lane"] == "follow_up_en"]
    assert all(c["recent"] and c["expected"][0] not in c["text"] for c in follow_ups)


@pytest.mark.parametrize(
    ("arm", "message"),
    [
        ("third_party", "--selector-url"),
        ("external_picker", "--model-base-url"),
        ("local", "--local-url"),
    ],
)
def test_an_arm_without_its_model_is_refused_so_its_label_cannot_mislead(arm, message):
    assert message in harness.arm_problem(arm, None, None, None)


def test_a_complete_arm_can_run(monkeypatch):
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "unit")
    assert harness.arm_problem("word", None, None, None) is None
    assert harness.arm_problem("local_picker", "m", "l", None) is None


def test_the_grade_counts_a_right_first_pick_and_a_sibling_first_pick():
    cases = [
        dict(lane="en", expected=["a"], family=["a", "b"]),
        dict(lane="en", expected=["a"], family=["a", "b"]),
    ]
    rows = [
        dict(picks=["a"], degraded=False, decision="picks"),
        dict(picks=["b", "a"], degraded=False, decision="picks"),
    ]
    entry = harness.grade(cases, rows)["en"]
    assert (entry["first_pick"], entry["first_pick_sibling"], entry["recall"]) == (1, 1, 2)


@pytest.mark.parametrize("split", ["development", "holdout"])
def test_each_split_has_complex_requests_that_need_several_skills(split):
    assets, cases = harness.load_cases(split)
    names = {a["name"] for a in assets}
    complex_cases = [c for c in cases if c["lane"].startswith("complex")]
    assert complex_cases
    for case in complex_cases:
        assert 3 <= len(case["expected"]) <= 5
        assert set(case["expected"]) <= names


def test_the_grade_reports_how_much_of_a_set_was_found():
    cases = [dict(lane="complex_en", expected=["a", "b"], family=[])]
    rows = [dict(picks=["a", "q"], degraded=False, decision="picks")]
    entry = harness.grade(cases, rows)["complex_en"]
    assert entry["found_share"] == 0.5
    assert entry["all_found"] == 0


def test_every_arm_can_be_set_through_the_gateways_own_settings():
    """Review of PR 285: a report must be reproducible through the gateway, and say who chose."""
    from relaymind.guidance import parse_guidance_settings

    selector = dict(name="team", version="1", url="http://127.0.0.1:9/select")
    for stages in harness.ARMS.values():
        settings = dict(stages, **selector) if stages.get("primary") == "http" else stages
        assert parse_guidance_settings(settings).primary == stages.get("primary", "builtin")


def test_an_external_arm_without_its_key_is_refused(monkeypatch):
    """Review of PR 285: a missing key would degrade every case to word match."""
    monkeypatch.delenv("UNIT_KEY", raising=False)
    message = harness.arm_problem("external_picker", "m", None, None, api_key_env="UNIT_KEY")
    assert "UNIT_KEY" in message
    monkeypatch.setenv("UNIT_KEY", "k")
    assert harness.arm_problem("external_picker", "m", None, None, api_key_env="UNIT_KEY") is None


def test_an_external_arm_without_a_model_name_is_refused(monkeypatch):
    """Review of PR 285: without a model the provider refuses every call."""
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "k")
    assert "--model" in harness.arm_problem("external_picker", "m", None, None, model=None)
    assert harness.arm_problem("external_picker", "m", None, None, model="x") is None


def test_request_options_are_json_values_and_follow_the_gateway_rules():
    assert harness.parse_options(["reasoning_effort=low", 'thinking={"type":"disabled"}']) == {
        "reasoning_effort": "low",
        "thinking": {"type": "disabled"},
    }
    with pytest.raises(ValueError):
        harness.parse_options(["model=other"])
