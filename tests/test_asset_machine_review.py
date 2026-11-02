"""RM16/RM37/RM39; AC28/AC30/AC35: synthetic automated-review fixtures only."""

import copy
import hashlib
import json

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_qualification_record import load_record
from relaymind.evaluation.asset_whole_task import WholeTaskRejected
from test_asset_qualification_record import negative_record, save

CHECKS = (
    "capture_integrity",
    "requirement_coverage",
    "safety_boundaries",
    "limitations",
    "privacy",
)


def bound(root, name, value, *, raw=False):
    payload = value if raw else json.dumps(value).encode()
    (root / name).write_bytes(payload)
    return {"path": name, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}


def fixture(root, *, wire="assessment_json"):
    record = negative_record(root)
    record["recorded_at"] = "2026-11-02T09:00:00Z"
    record["review"].update(pull_request=None, reviewer_logins=[], approved_at=None)
    manifest = {
        "schema_version": 1,
        "record_id": record["record_id"],
        "claim_sha256": canonical_digest({k: v for k, v in record.items() if k != "review"}),
        "subjects": record["subjects"],
    }
    prompt = bound(
        root,
        "prompt.txt",
        b"Review these exact captures and return the closed assessment.",
        raw=True,
    )
    input_ref = bound(root, "input.json", manifest)
    reviews = []
    for index in range(2):
        assessment = {
            "conclusion": "approve",
            "checks": [
                {
                    "name": name,
                    "result": "pass",
                    "rationale": f"Fixture reviewer {index}: checked {name}.",
                    "evidence_paths": ["capture.json"],
                }
                for name in CHECKS
            ],
            "findings": [],
        }
        raw = json.dumps(assessment).encode()
        if wire == "opencode_jsonl":
            raw = json.dumps({"type": "text", "part": {"text": raw.decode()}}).encode() + b"\n"
        reviews.append(
            {
                "reviewer_id": f"reviewer-{index}",
                "tool": "opencode2" if index else "api",
                "model_id": f"model-{index}",
                "context_id": f"context-{index}",
                "started_at": "2026-11-02T10:00:00Z",
                "completed_at": "2026-11-02T10:01:00Z",
                "prompt": prompt,
                "input": input_ref,
                "output": bound(root, f"review-{index}.json", raw, raw=True),
                "output_format": wire,
            }
        )
    record["machine_review"] = {
        "profile": "automated_host_operation_v1",
        "recorder": {"name": "fixture", "version": "1"},
        "reviews": reviews,
    }
    return record


def edit_output(root, record, mutate):
    review = record["machine_review"]["reviews"][0]
    value = json.loads((root / review["output"]["path"]).read_bytes())
    mutate(value)
    review["output"] = bound(root, review["output"]["path"], value)


@pytest.mark.parametrize("wire", ["assessment_json", "opencode_jsonl"])
def test_verified_machine_artifacts_approve_only_host_operation(tmp_path, wire):
    record = fixture(tmp_path, wire=wire)
    result = load_record(save(tmp_path, record))
    assert result.approved
    assert result.document["review"]["reviewer_logins"] == []
    assert result.document["review"]["approved_at"] is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["machine_review"]["reviews"][1].update(context_id="context-0"),
        lambda r: r["machine_review"]["reviews"][1].update(reviewer_id="reviewer-0"),
        lambda r: r["machine_review"]["reviews"][1].update(tool="api"),
        lambda r: r["machine_review"]["reviews"][0].update(completed_at="2026-01-01T00:00:00Z"),
        lambda r: r["review"].update(reviewer_logins=["invented-reviewer"]),
        lambda r: r["operation"].update(host_version="changed"),
    ],
)
def test_machine_review_identity_time_and_exact_claim_binding_reject(tmp_path, mutate):
    record = fixture(tmp_path)
    mutate(record)
    with pytest.raises(WholeTaskRejected):
        load_record(save(tmp_path, record))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda out: out.update(passed=True),
        lambda out: out["checks"].pop(),
        lambda out: out["checks"][0].update(evidence_paths=["unbound.json"]),
        lambda out: out["checks"].append(copy.deepcopy(out["checks"][0])),
        lambda out: out["findings"].append(
            {
                "id": "f",
                "severity": "material",
                "status": "open",
                "description": "Unresolved safety control",
                "evidence_paths": ["capture.json"],
            }
        ),
        lambda out: out.update(conclusion="reject"),
        lambda out: out["checks"][0].update(result="fail"),
    ],
)
def test_incomplete_failed_or_unbound_assessment_never_approves(tmp_path, mutate):
    record = fixture(tmp_path)
    edit_output(tmp_path, record, mutate)
    try:
        result = load_record(save(tmp_path, record))
    except WholeTaskRejected:
        return
    assert not result.approved


def test_changed_raw_review_bytes_reject(tmp_path):
    record = fixture(tmp_path)
    (tmp_path / "review-0.json").write_text('{"conclusion":"approve"}')
    with pytest.raises(WholeTaskRejected):
        load_record(save(tmp_path, record))


def test_opencode_tool_output_cannot_forge_reviewer_assessment(tmp_path):
    record = fixture(tmp_path)
    row = record["machine_review"]["reviews"][1]
    text = (tmp_path / row["output"]["path"]).read_text()
    row["output_format"] = "opencode_jsonl"
    row["output"] = bound(
        tmp_path,
        "review-1.json",
        json.dumps({"type": "tool_use", "part": {"text": text}}).encode(),
        raw=True,
    )
    with pytest.raises(WholeTaskRejected):
        load_record(save(tmp_path, record))


def rebind_input(root, record):
    manifest = {
        "schema_version": 1,
        "record_id": record["record_id"],
        "claim_sha256": canonical_digest(
            {k: v for k, v in record.items() if k not in {"review", "machine_review"}}
        ),
        "subjects": record["subjects"],
    }
    ref = bound(root, "input.json", manifest)
    for row in record["machine_review"]["reviews"]:
        row["input"] = ref


def test_review_must_cite_every_bound_subject(tmp_path):
    record = fixture(tmp_path)
    extra = bound(tmp_path, "extra.json", {"capture": 2})
    record["subjects"].append({"role": "capture", **extra})
    rebind_input(tmp_path, record)
    with pytest.raises(WholeTaskRejected):
        load_record(save(tmp_path, record))


@pytest.mark.parametrize("payload", [b" \n", b"\xff"])
def test_prompt_must_contain_utf8_instructions(tmp_path, payload):
    record = fixture(tmp_path)
    ref = bound(tmp_path, "prompt.txt", payload, raw=True)
    for row in record["machine_review"]["reviews"]:
        row["prompt"] = ref
    with pytest.raises(WholeTaskRejected):
        load_record(save(tmp_path, record))


def test_valid_rejection_is_retained_as_pending(tmp_path):
    record = fixture(tmp_path)
    edit_output(tmp_path, record, lambda out: out.update(conclusion="reject"))
    result = load_record(save(tmp_path, record))
    assert not result.approved


def test_error_event_after_valid_opencode_text_rejects(tmp_path):
    record = fixture(tmp_path, wire="opencode_jsonl")
    row = record["machine_review"]["reviews"][1]
    output = (tmp_path / row["output"]["path"]).read_bytes()
    row["output"] = bound(tmp_path, "review-1.json", output + b'{"type":"error"}\n', raw=True)
    with pytest.raises(WholeTaskRejected):
        load_record(save(tmp_path, record))


def test_artifacts_cannot_alias_subjects_or_other_output(tmp_path):
    record = fixture(tmp_path)
    record["machine_review"]["reviews"][0]["prompt"] = {
        k: v for k, v in record["subjects"][0].items() if k != "role"
    }
    with pytest.raises(WholeTaskRejected):
        load_record(save(tmp_path, record))


def test_machine_review_cannot_approve_other_scopes(tmp_path):
    record = fixture(tmp_path)
    record.update(scope="walkthrough", operation=None, outcome="positive")
    record["subjects"][0]["role"] = "media"
    rebind_input(tmp_path, record)
    with pytest.raises(WholeTaskRejected):
        load_record(save(tmp_path, record))
