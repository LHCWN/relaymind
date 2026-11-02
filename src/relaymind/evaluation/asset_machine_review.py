"""Bound automated host-operation reviews (RM16/RM37/RM39; AC28/AC30/AC35).

These checks verify recorded provenance and actual critique bytes. They cannot
cryptographically authenticate a service, separate context, or sound judgment.
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_benchmark_io import validate_closed_document
from relaymind.evaluation.asset_corpus_io import parse_json
from relaymind.evaluation.asset_whole_task import WholeTaskRejected

ARTIFACT_BYTES = 256 * 1024
INPUT_BYTES = 4 * 1024 * 1024
_CHECKS = frozenset(
    ("capture_integrity", "requirement_coverage", "safety_boundaries", "limitations", "privacy")
)


def _reject() -> None:
    raise WholeTaskRejected("machine_review_invalid")


def _assessment(payload: bytes, format_name: str, schema: Mapping) -> Mapping:
    try:
        if format_name == "opencode_jsonl":
            parts = []
            for line in payload.splitlines():
                if not line.strip():
                    continue
                event = parse_json(line)
                if not isinstance(event, dict) or event.get("type") == "error":
                    _reject()
                if event.get("type") == "text":
                    part = event.get("part")
                    if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                        _reject()
                    parts.append(part["text"])
            payload = "".join(parts).encode("utf-8")
        assessment = parse_json(payload)
        validate_closed_document(
            {"$ref": "#/$defs/MachineReviewAssessment", "$defs": schema["$defs"]},
            assessment,
            "machine_review_invalid",
        )
        return assessment
    except (ValueError, UnicodeError) as exc:
        raise WholeTaskRejected("machine_review_invalid") from exc


def verify_machine_review(
    record: Mapping,
    root: Path,
    schema: Mapping,
    read_bound: Callable[[Path, Mapping, int], bytes],
    time: Callable[[str], Any],
) -> bool:
    """Return approval only after both actual outputs and every binding verify."""
    profile = record.get("machine_review")
    if profile is None:
        return False
    review = record["review"]
    if record["scope"] != "host_operation" or review["reviewer_logins"] or review["approved_at"]:
        _reject()
    reviews = profile["reviews"]
    if any(len({row[key] for row in reviews}) != 2 for key in ("reviewer_id", "context_id")):
        _reject()
    if not any(row["tool"] == "opencode2" for row in reviews):
        _reject()
    expected_input = {
        "schema_version": 1,
        "record_id": record["record_id"],
        "claim_sha256": canonical_digest(
            {key: value for key, value in record.items() if key not in {"review", "machine_review"}}
        ),
        "subjects": record["subjects"],
    }
    subject_paths = {subject["path"] for subject in record["subjects"]}
    artifacts: dict[str, tuple[str, Mapping]] = {}
    output_hashes: set[str] = set()
    approved = True
    for row in reviews:
        if not time(record["recorded_at"]) <= time(row["started_at"]) <= time(row["completed_at"]):
            _reject()
        payloads = {}
        for role in ("prompt", "input", "output"):
            ref = row[role]
            path = ref["path"]
            previous = artifacts.get(path)
            if (
                path in subject_paths
                or previous is not None
                and (role == "output" or previous != (role, ref))
            ):
                _reject()
            artifacts[path] = role, ref
            limit = INPUT_BYTES if role == "input" else ARTIFACT_BYTES
            payloads[role] = read_bound(root, ref, limit)
        if row["output"]["sha256"] in output_hashes:
            _reject()
        output_hashes.add(row["output"]["sha256"])
        try:
            if not payloads["prompt"].decode("utf-8").strip():
                _reject()
            manifest = parse_json(payloads["input"])
            if canonical_digest(manifest) != canonical_digest(expected_input):
                _reject()
        except ValueError as exc:
            raise WholeTaskRejected("machine_review_invalid") from exc
        assessment = _assessment(payloads["output"], row["output_format"], schema)
        checks, findings = assessment["checks"], assessment["findings"]
        if {check["name"] for check in checks} != _CHECKS:
            _reject()
        if len({finding["id"] for finding in findings}) != len(findings):
            _reject()
        cited = set()
        for item in (*checks, *findings):
            text = item.get("rationale", item.get("description", ""))
            if not text.strip() or not set(item["evidence_paths"]) <= subject_paths:
                _reject()
            cited.update(item["evidence_paths"])
        if cited != subject_paths:
            _reject()
        approved &= assessment["conclusion"] == "approve"
        approved &= all(check["result"] == "pass" for check in checks)
        approved &= not any(
            finding["severity"] == "material" and finding["status"] == "open"
            for finding in findings
        )
    return approved
