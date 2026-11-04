"""RM16/RM44; AC36: internal binding checks never certify experiment completion."""

import hashlib
import json

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_automated_record import load_automated_record
from test_asset_automated_host import bound_protocol, frozen_inputs
from whole_task_helpers import ordered_rows, report_for, row


def write(root, name, value):
    payload = value if isinstance(value, bytes) else json.dumps(value).encode()
    (root / name).write_bytes(payload)
    return dict(path=name, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))


def fixture(root):
    lock, freeze, tasks = frozen_inputs()
    p = bound_protocol(lock, freeze, tasks)
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
        payload = {"started_at": "2026-11-02T10:00:00Z"} if n == 0 else {"fixture": n}
        item = write(root, f"capture-{n}.json", payload)
        ref.update(sha256=item["sha256"], bytes=item["bytes"])
        evidence.append(dict(id=ref["id"], **item))
    dispatch_refs = []
    for index, run_row in enumerate(report["rows"]):
        identity = f"host-dispatch:{run_row['task_id']}-{run_row['arm']}"
        dispatch_ref = write(
            root,
            f"host-dispatch-{index}.json",
            {
                "schema_version": 1,
                "task_id": run_row["task_id"],
                "arm": run_row["arm"],
                "started_at": "2026-11-02T10:00:00Z",
            },
        )
        report["evidence"].append(dict(id=identity, provenance="observed", **dispatch_ref))
        run_row["evidence_ids"] = sorted([*run_row["evidence_ids"], identity])
        evidence.append(dict(id=identity, **dispatch_ref))
        dispatch_refs.append(dispatch_ref)
    report["evidence"].sort(key=lambda item: item["id"])
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
            "capture": dispatch_refs[0],
        },
        judging={
            key: write(root, f"{key}.json", {"fixture": key})
            for key in ("blind_manifest", "blind_map", "judgments", "usage", "captures")
        },
        limitations=["Fixture only; no experiment completion or acceptance."],
        reviews=[],
    )
    record["judging"]["captures"] = write(root, "captures.json", {"files": []})
    for n, reviewer in enumerate(p["reviewers"]):
        record["reviews"].append(
            dict(
                id=reviewer["id"],
                model=reviewer["model"],
                context_id=f"ctx-{n}",
                started_at="2026-11-02T11:00:00Z",
                completed_at="2026-11-02T11:01:00Z",
                prompt=prompt,
                output=write(
                    root, f"review-output-{n}.txt", b"Material issues remain. Do not approve."
                ),
                usage=write(
                    root, f"review-usage-{n}.json", {"input_tokens": None, "output_tokens": None}
                ),
            )
        )
    rebind_review_inputs(root, record)
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
        "escaped_review_path",
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
    if mutation == "escaped_review_path":
        record["reviews"][0]["output"]["path"] = "../outside"
    write(tmp_path, "record.json", record)
    with pytest.raises(ValueError):
        load_automated_record(tmp_path / "record.json")


def rebind_review_inputs(root, record):
    """Keep review claim bindings valid so regressions reach the intended check."""
    claim = canonical_digest({key: value for key, value in record.items() if key != "reviews"})
    for index, review in enumerate(record["reviews"]):
        review["input"] = write(
            root, f"review-input-{index}.json", {"schema_version": 1, "claim_sha256": claim}
        )
    write(root, "record.json", record)


def test_dispatch_capture_must_be_report_evidence(tmp_path):
    """RM16/AC36: a separately authored earlier timestamp is not report evidence."""
    record = fixture(tmp_path)
    record["first_dispatch"] = {
        "at": "2026-11-02T09:30:00Z",
        "capture": write(
            tmp_path, "unrelated-dispatch.json", {"started_at": "2026-11-02T09:30:00Z"}
        ),
    }
    rebind_review_inputs(tmp_path, record)
    with pytest.raises(ValueError):
        load_automated_record(tmp_path / "record.json")


@pytest.mark.parametrize(
    "field", ["preregistration", "first_dispatch", "review_start", "review_end"]
)
@pytest.mark.parametrize("value", [None, 5, [], {}])
def test_nonstring_record_timestamps_are_controlled_rejections(tmp_path, monkeypatch, field, value):
    """RM16/AC36: corrupted times reject as ValueError, never leak TypeError."""
    import relaymind.evaluation.asset_automated_record as module

    observed = []
    original = module._timestamp

    def observe(item):
        observed.append(item)
        return original(item)

    monkeypatch.setattr(module, "_timestamp", observe)
    record = fixture(tmp_path)
    if field == "preregistration":
        record[field]["committed_at"] = value
    elif field == "first_dispatch":
        record[field]["at"] = value
    else:
        record["reviews"][0]["started_at" if field == "review_start" else "completed_at"] = value
    rebind_review_inputs(tmp_path, record)
    with pytest.raises(ValueError):
        load_automated_record(tmp_path / "record.json")
    assert any(type(item) is type(value) and item == value for item in observed)


def test_dispatch_match_uses_bytes_not_private_relative_path(tmp_path):
    """RM16/AC36: a materialized copy may have another path, but identical bytes."""
    record = fixture(tmp_path)
    original = tmp_path / record["first_dispatch"]["capture"]["path"]
    record["first_dispatch"]["capture"] = write(
        tmp_path, "dispatch-copy.json", original.read_bytes()
    )
    rebind_review_inputs(tmp_path, record)
    assert load_automated_record(tmp_path / "record.json")["bindings_verified"]


def test_used_upstream_capture_is_not_whole_row_dispatch(tmp_path):
    """RM16/AC36: even real used model-start evidence cannot substitute for row dispatch."""
    record = fixture(tmp_path)
    evidence = json.loads((tmp_path / "evidence.json").read_bytes())["files"]
    upstream = next(item for item in evidence if item["path"] == "capture-0.json")
    record["first_dispatch"]["capture"] = {
        key: value for key, value in upstream.items() if key != "id"
    }
    rebind_review_inputs(tmp_path, record)
    with pytest.raises(ValueError):
        load_automated_record(tmp_path / "record.json")


@pytest.mark.parametrize(
    "mutation", ["extra_field", "wrong_task", "wrong_arm", "not_observed", "missing_row"]
)
def test_all_row_dispatches_use_existing_host_contract(tmp_path, mutation):
    """RM16/AC36: another row's malformed dispatch cannot hide behind a valid first one."""
    record = fixture(tmp_path)
    report = json.loads((tmp_path / "report.json").read_bytes())
    manifest = json.loads((tmp_path / "evidence.json").read_bytes())
    target = next(ref for ref in report["evidence"] if ref["path"] == "host-dispatch-1.json")
    if mutation == "not_observed":
        target["provenance"] = "fixture"
    elif mutation == "missing_row":
        for run_row in report["rows"]:
            run_row["evidence_ids"] = [
                identity for identity in run_row["evidence_ids"] if identity != target["id"]
            ]
    else:
        capture = json.loads((tmp_path / target["path"]).read_bytes())
        capture.update(
            {
                "extra_field": {"extra": True},
                "wrong_task": {"task_id": "different"},
                "wrong_arm": {"arm": "different"},
            }[mutation]
        )
        ref = write(tmp_path, target["path"], capture)
        target.update(ref)
        for item in manifest["files"]:
            if item["id"] == target["id"]:
                item.update(ref)
    record["report"] = write(tmp_path, "report.json", report)
    record["evidence_manifest"] = write(tmp_path, "evidence.json", manifest)
    rebind_review_inputs(tmp_path, record)
    with pytest.raises(ValueError):
        load_automated_record(tmp_path / "record.json")
