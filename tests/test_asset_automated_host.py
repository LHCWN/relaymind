"""RM16/RM35/RM37/RM44; AC26/AC28/AC36: automated evaluation boundary."""

import copy
import json
from pathlib import Path

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_automated_host import (
    AutomatedHostRejected,
    select_proposal,
    validate_protocol,
)
from whole_task_helpers import freeze_doc, tasks_doc

H = "a" * 64
LOCK = Path(__file__).parents[1] / "evaluation/assets/owner-run-260/lock.json"


def protocol():
    def machine(name):
        return dict(id=name, model="model", prompt_sha256=H, temperature=0)

    return dict(
        schema_version=1,
        profile="automated_host",
        source_tasks_digest=H,
        source_lock_digest=H,
        freeze_digest=H,
        tasks_digest=H,
        driver=dict(
            provider="provider", model="model", prompt_sha256=H, temperature=0, max_calls=1
        ),
        judges=[machine("a"), machine("b"), machine("adjudicator")],
        reviewers=[machine("review-a"), machine("review-b")],
        per_generation_seconds=90,
        row_token_limit=250000,
        total_token_limit=13000000,
    )


def frozen_inputs():
    """The real source lock with a one-task freeze and task wrapper bound to it."""
    lock = json.loads(LOCK.read_text())
    freeze = freeze_doc(task_ids=("t001",))
    freeze["corpus_lock_digest"] = canonical_digest(lock)
    return lock, freeze, tasks_doc(freeze, lanes=("en_to_en",))


def bound_protocol(lock, freeze, source_tasks, tasks=None):
    """A protocol whose input digests match exactly these documents."""
    return dict(
        protocol(),
        source_tasks_digest=canonical_digest(source_tasks),
        source_lock_digest=canonical_digest(lock),
        freeze_digest=canonical_digest(freeze),
        tasks_digest=canonical_digest(source_tasks if tasks is None else tasks),
    )


def test_protocol_is_closed_and_detached():
    value = protocol()
    result = validate_protocol(value)
    result["driver"]["model"] = "changed"
    assert value["driver"]["model"] == "model"
    value["approved_by_human"] = True
    with pytest.raises(AutomatedHostRejected):
        validate_protocol(value)


@pytest.mark.parametrize(
    "edit",
    [
        lambda p: p["reviewers"].pop(),
        lambda p: p["reviewers"][0].update(id="a"),
        lambda p: p["driver"].update(max_calls=True),
        lambda p: p.update(row_token_limit=14000000),
        lambda p: p.update(per_generation_seconds=float("inf")),
        lambda p: p.update(source_tasks_digest="A" * 64),
    ],
)
def test_reject_invalid_protocol(edit):
    value = protocol()
    edit(value)
    with pytest.raises(AutomatedHostRejected):
        validate_protocol(value)


def test_select_only_actual_candidate_metadata():
    candidate = dict(asset=dict(asset_id="actual", content_hash=H, description="data"), rank=1)
    chosen = select_proposal(dict(candidate_id="actual", content_hash=H), [candidate])
    assert chosen == candidate
    chosen["asset"]["description"] = "changed"
    assert candidate["asset"]["description"] == "data"
    assert select_proposal(dict(candidate_id=None, content_hash=None), []) is None
    for proposal in [
        dict(candidate_id="invented", content_hash=H),
        dict(candidate_id=None, content_hash=H),
        dict(candidate_id="actual", content_hash="b" * 64),
        dict(candidate_id="actual", content_hash=H, instructions="injected"),
    ]:
        with pytest.raises(AutomatedHostRejected):
            select_proposal(proposal, [candidate])
    with pytest.raises(AutomatedHostRejected):
        select_proposal(
            dict(candidate_id="actual", content_hash=H), [candidate, copy.deepcopy(candidate)]
        )


def test_invalid_unicode_identity_is_refused():
    """RM16/RM35; AC26: malformed identity cannot escape the boundary error."""
    value = protocol()
    value["driver"]["model"] = "\ud800"
    with pytest.raises(AutomatedHostRejected):
        validate_protocol(value)
