"""RM16/RM44; AC36: internal binding checks never certify experiment completion."""

import hashlib
import json
from pathlib import Path

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_automated_record import load_automated_record
from test_asset_automated_host import protocol
from whole_task_helpers import freeze_doc, ordered_rows, report_for, row, tasks_doc


def write(root, name, value):
    payload = value if isinstance(value, bytes) else json.dumps(value).encode()
    (root / name).write_bytes(payload)
    return dict(path=name, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))


def fixture(root):
    lock = json.loads(
        (Path(__file__).parents[1] / "evaluation/assets/owner-run-260/lock.json").read_text()
    )
    freeze = freeze_doc(task_ids=("t001",))
    freeze["corpus_lock_digest"] = canonical_digest(lock)
    tasks = tasks_doc(freeze, lanes=("en_to_en",))
    p = protocol()
    p.update(
        source_tasks_digest=canonical_digest(tasks),
        source_lock_digest=canonical_digest(lock),
        tasks_digest=canonical_digest(tasks),
        freeze_digest=canonical_digest(freeze),
    )
    prompt = write(root, "review-prompt.txt", b"Inspect actual bound evidence; report findings.")
    for reviewer in p["reviewers"]:
        reviewer["prompt_sha256"] = prompt["sha256"]
    report = report_for(freeze, tasks, ordered_rows(tasks, row))
    for r in report["rows"]:
        for a in r["attempts"]:
            a["started_at"] = "2026-11-02T10:00:00Z"
            a["ended_at"] = "2026-11-02T10:00:01Z"
    evidence = []
    for n, ref in enumerate(report["evidence"]):
        item = write(root, f"capture-{n}.json", {"fixture": n})
        ref.update(sha256=item["sha256"], bytes=item["bytes"])
        evidence.append(dict(id=ref["id"], **item))
    inputs = {"fixture": "not acceptance"}
    report["input_manifest"] = dict(
        path=None, digest=canonical_digest(inputs), bytes=len(json.dumps(inputs).encode())
    )
    record = dict(
        schema_version=1,
        profile="automated_host",
        protocol=write(root, "protocol.json", p),
        source_tasks=write(root, "source-tasks.json", tasks),
        source_lock=write(root, "lock.json", lock),
        tasks=write(root, "tasks.json", tasks),
        freeze=write(root, "freeze.json", freeze),
        report=write(root, "report.json", report),
        input_manifest=write(root, "inputs.json", inputs),
        evidence_manifest=write(root, "evidence.json", {"files": evidence}),
        code_manifest=write(
            root, "code.json", {"files": [write(root, "runner.py", b"# fixture code\n")]}
        ),
        preregistration={"commit": "a" * 40, "committed_at": "2026-11-02T09:00:00Z"},
        first_dispatch={
            "at": "2026-11-02T10:00:00Z",
            "capture": write(root, "dispatch.json", {"started_at": "2026-11-02T10:00:00Z"}),
        },
        judging={
            key: write(root, f"{key}.json", {"fixture": key})
            for key in ("blind_manifest", "blind_map", "judgments", "usage", "captures")
        },
        limitations=["Fixture only; no experiment completion or acceptance."],
        reviews=[],
    )
    record["judging"]["captures"] = write(root, "captures.json", {"files": []})
    claim = canonical_digest({k: v for k, v in record.items() if k != "reviews"})
    for n, reviewer in enumerate(p["reviewers"]):
        record["reviews"].append(
            dict(
                id=reviewer["id"],
                model=reviewer["model"],
                context_id=f"ctx-{n}",
                started_at="2026-11-02T11:00:00Z",
                completed_at="2026-11-02T11:01:00Z",
                prompt=prompt,
                input=write(
                    root, f"review-input-{n}.json", {"schema_version": 1, "claim_sha256": claim}
                ),
                output=write(
                    root, f"review-output-{n}.txt", b"Material issues remain. Do not approve."
                ),
                usage=write(
                    root, f"review-usage-{n}.json", {"input_tokens": None, "output_tokens": None}
                ),
            )
        )
    write(root, "record.json", record)
    return record


def test_bindings_are_not_completion_or_approval(tmp_path):
    fixture(tmp_path)
    result = load_automated_record(tmp_path / "record.json")
    assert result["bindings_verified"] is True
    assert result["completion_verified"] is False
    assert result["planned_rows"] == 3


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_capture",
        "changed_capture",
        "fake_approval",
        "late_prereg",
        "wrong_model",
        "duplicate_context",
        "changed_claim",
        "dropped_row",
    ],
)
def test_inconsistent_record_rejects(tmp_path, mutation):
    record = fixture(tmp_path)
    if mutation == "missing_capture":
        (tmp_path / "capture-0.json").unlink()
    if mutation == "changed_capture":
        (tmp_path / "capture-0.json").write_text("{}")
    if mutation == "fake_approval":
        record["approved"] = True
    if mutation == "late_prereg":
        record["preregistration"]["committed_at"] = "2026-11-02T12:00:00Z"
    if mutation == "wrong_model":
        record["reviews"][0]["model"] = "different"
    if mutation == "duplicate_context":
        record["reviews"][1]["context_id"] = "ctx-0"
    if mutation == "changed_claim":
        record["limitations"] = ["No limitations"]
    if mutation == "dropped_row":
        report = json.loads((tmp_path / "report.json").read_bytes())
        report["rows"].pop()
        record["report"] = write(tmp_path, "report.json", report)
    write(tmp_path, "record.json", record)
    with pytest.raises(ValueError):
        load_automated_record(tmp_path / "record.json")


def test_review_artifact_path_cannot_escape_root(tmp_path):
    record = fixture(tmp_path)
    record["reviews"][0]["output"]["path"] = "../outside"
    write(tmp_path, "record.json", record)
    with pytest.raises(ValueError):
        load_automated_record(tmp_path / "record.json")
