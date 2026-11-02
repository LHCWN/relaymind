"""RM16/RM44; AC31/AC36: executable automated preregistration workflow."""

import json
import subprocess
import sys
from pathlib import Path

from relaymind.canonical import canonical_digest
from test_asset_automated_host import protocol
from whole_task_helpers import freeze_doc, tasks_doc

ROOT = Path(__file__).resolve().parents[1]


def test_preregister_and_reject_changed_source(tmp_path):
    lock = json.loads((ROOT / "evaluation/assets/owner-run-260/lock.json").read_text())
    freeze = freeze_doc(task_ids=("t001",))
    freeze["corpus_lock_digest"] = canonical_digest(lock)
    tasks = tasks_doc(freeze, lanes=("en_to_en",))
    p = protocol()
    p.update(
        source_tasks_digest=canonical_digest(tasks),
        source_lock_digest=canonical_digest(lock),
        freeze_digest=canonical_digest(freeze),
        tasks_digest=canonical_digest(tasks),
    )
    inputs = dict(protocol=p, source_tasks=tasks, source_lock=lock, tasks=tasks, freeze=freeze)
    command = [sys.executable, str(ROOT / "scripts/asset_automated_host.py")]
    for key, value in inputs.items():
        path = tmp_path / (key + ".json")
        path.write_text(json.dumps(value))
        command.extend(["--" + key.replace("_", "-"), str(path)])
    output = tmp_path / "preregistration.json"
    command.extend(["--output", str(output)])
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    record = json.loads(output.read_text())
    assert record["protocol_digest"] == canonical_digest(p)
    assert record["execution_status"] == "not_run"
    assert record["host_preregistration"]["planned_rows"] == 3
    original = output.read_bytes()
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert output.read_bytes() == original
    (tmp_path / "source_lock.json").write_text("{}")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0


def test_source_task_change_cannot_hide_behind_new_protocol_digest():
    """RM40/RM44; AC31/AC36: new freeze cannot change the frozen task meaning."""
    import copy

    import pytest

    from relaymind.evaluation.asset_automated_host import AutomatedHostRejected, bind_inputs

    lock = json.loads((ROOT / "evaluation/assets/owner-run-260/lock.json").read_text())
    freeze = freeze_doc(task_ids=("t001",))
    freeze["corpus_lock_digest"] = canonical_digest(lock)
    source = tasks_doc(freeze, lanes=("en_to_en",))
    tasks = copy.deepcopy(source)
    tasks["tasks"][0]["success_rubric_evidence_id"] = "changed-rubric"
    freeze["rubric_evidence_ids"] = ["changed-rubric"]
    tasks["freeze_digest"] = canonical_digest(freeze)
    p = protocol()
    p.update(
        source_tasks_digest=canonical_digest(source),
        source_lock_digest=canonical_digest(lock),
        freeze_digest=canonical_digest(freeze),
        tasks_digest=canonical_digest(tasks),
    )
    with pytest.raises(AutomatedHostRejected, match="source_tasks_changed"):
        bind_inputs(p, source, lock, tasks, freeze)
