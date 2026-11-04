"""RM16/RM44; AC31/AC36: executable automated preregistration workflow."""

import copy
import json

import pytest

from helpers import run_script
from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_automated_host import AutomatedHostRejected, bind_inputs
from test_asset_automated_host import bound_protocol, frozen_inputs


def test_preregister_and_reject_changed_source(tmp_path):
    lock, freeze, tasks = frozen_inputs()
    p = bound_protocol(lock, freeze, tasks)
    inputs = dict(protocol=p, source_tasks=tasks, source_lock=lock, tasks=tasks, freeze=freeze)
    command = []
    for key, value in inputs.items():
        path = tmp_path / (key + ".json")
        path.write_text(json.dumps(value))
        command.extend(["--" + key.replace("_", "-"), str(path)])
    output = tmp_path / "preregistration.json"
    command.extend(["--output", str(output)])
    result = run_script("asset_automated_host", *command)
    assert result.returncode == 0, result.stderr
    record = json.loads(output.read_text())
    assert record["protocol_digest"] == canonical_digest(p)
    assert record["execution_status"] == "not_run"
    assert record["host_preregistration"]["planned_rows"] == 3
    original = output.read_bytes()
    result = run_script("asset_automated_host", *command)
    assert result.returncode != 0
    assert output.read_bytes() == original
    (tmp_path / "source_lock.json").write_text("{}")
    result = run_script("asset_automated_host", *command)
    assert result.returncode != 0


def test_source_task_change_cannot_hide_behind_new_protocol_digest():
    """RM40/RM44; AC31/AC36: new freeze cannot change the frozen task meaning."""
    lock, freeze, source = frozen_inputs()
    tasks = copy.deepcopy(source)
    tasks["tasks"][0]["success_rubric_evidence_id"] = "changed-rubric"
    freeze["rubric_evidence_ids"] = ["changed-rubric"]
    tasks["freeze_digest"] = canonical_digest(freeze)
    p = bound_protocol(lock, freeze, source, tasks)
    with pytest.raises(AutomatedHostRejected, match="source_tasks_changed"):
        bind_inputs(p, source, lock, tasks, freeze)
