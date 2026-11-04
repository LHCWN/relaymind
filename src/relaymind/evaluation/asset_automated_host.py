"""Closed automated experiment inputs (RM16/RM35/RM37/RM44; spec 12.6.7).

These helpers validate proposals only. They grant no direct-action authority,
perform no model calls and establish no measured or qualified result.
"""

import copy
import math
import re
from collections.abc import Mapping, Sequence

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_host_runner import host_preregistration
from relaymind.evaluation.asset_whole_task import validate_whole_task

_HASH = re.compile(r"[0-9a-f]{64}\Z")


class AutomatedHostRejected(ValueError):
    """The frozen protocol or proposed candidate is invalid."""


def _closed(value, keys):
    if not isinstance(value, Mapping) or set(value) != set(keys.split()):
        raise AutomatedHostRejected("fields")


def _id(value):
    if not isinstance(value, str) or not value:
        raise AutomatedHostRejected("identity")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise AutomatedHostRejected("identity") from None
    if size > 256:
        raise AutomatedHostRejected("identity")


def _hash(value):
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise AutomatedHostRejected("digest")


def _positive(value):
    if type(value) is not int or value <= 0:
        raise AutomatedHostRejected("limit")


def _zero_temperature(entry):
    if type(entry["temperature"]) not in (int, float) or entry["temperature"] != 0:
        raise AutomatedHostRejected("temperature")


def validate_protocol(value: Mapping) -> dict:
    """Validate the closed AutomatedHostProtocolV1 and return a detached copy."""
    _closed(
        value,
        "schema_version profile source_tasks_digest source_lock_digest freeze_digest tasks_digest "
        "driver judges reviewers per_generation_seconds row_token_limit total_token_limit",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["profile"] != "automated_host"
    ):
        raise AutomatedHostRejected("profile")
    for key in ("source_tasks_digest", "source_lock_digest", "freeze_digest", "tasks_digest"):
        _hash(value[key])
    driver = value["driver"]
    _closed(driver, "provider model prompt_sha256 temperature max_calls")
    _id(driver["provider"])
    _id(driver["model"])
    _hash(driver["prompt_sha256"])
    _positive(driver["max_calls"])
    _zero_temperature(driver)
    seen = {"driver"}
    for key in ("judges", "reviewers"):
        machines = value[key]
        if not isinstance(machines, list) or (
            len(machines) != 3 if key == "judges" else len(machines) < 2
        ):
            raise AutomatedHostRejected("review_count")
        for machine in machines:
            _closed(machine, "id model prompt_sha256 temperature")
            _id(machine["id"])
            _id(machine["model"])
            _hash(machine["prompt_sha256"])
            if machine["id"] in seen:
                raise AutomatedHostRejected("duplicate_identity")
            seen.add(machine["id"])
            _zero_temperature(machine)
    seconds = value["per_generation_seconds"]
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise AutomatedHostRejected("deadline")
    for key in ("row_token_limit", "total_token_limit"):
        _positive(value[key])
    if value["row_token_limit"] > value["total_token_limit"]:
        raise AutomatedHostRejected("budget")
    return copy.deepcopy(dict(value))


def select_proposal(proposal: Mapping, candidates: Sequence[Mapping]) -> dict | None:
    """Return actual displayed metadata or abstention; never create a choice."""
    _closed(proposal, "candidate_id content_hash")
    candidate_id, digest = proposal["candidate_id"], proposal["content_hash"]
    by_id = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or not isinstance(candidate.get("asset"), Mapping):
            raise AutomatedHostRejected("candidate")
        asset = candidate["asset"]
        identity = asset.get("asset_id")
        _id(identity)
        _hash(asset.get("content_hash"))
        if identity in by_id:
            raise AutomatedHostRejected("duplicate_candidate")
        by_id[identity] = candidate
    if candidate_id is None and digest is None:
        return None
    _id(candidate_id)
    _hash(digest)
    candidate = by_id.get(candidate_id)
    if candidate is None or candidate["asset"]["content_hash"] != digest:
        raise AutomatedHostRejected("candidate_mismatch")
    return copy.deepcopy(dict(candidate))


def bind_inputs(
    protocol: Mapping,
    source_tasks: Mapping,
    source_lock: Mapping,
    tasks: Mapping,
    freeze: Mapping,
) -> dict:
    """Bind new wrappers without changing original task identities or rubrics.

    This checks identity, not authenticity, original catalog availability or
    acceptance qualification. The lock's self-digest excludes its digest field.
    """
    protocol = validate_protocol(protocol)
    for key, value in (
        ("source_tasks_digest", source_tasks),
        ("source_lock_digest", source_lock),
        ("tasks_digest", tasks),
        ("freeze_digest", freeze),
    ):
        if protocol[key] != canonical_digest(value):
            raise AutomatedHostRejected("input_digest")
    lock_digest = canonical_digest({k: v for k, v in source_lock.items() if k != "digest"})
    if source_lock.get("digest") != lock_digest or source_lock.get("state") != "frozen":
        raise AutomatedHostRejected("source_lock")
    if source_tasks.get("corpus_lock_digest") != canonical_digest(source_lock):
        raise AutomatedHostRejected("source_lock_binding")
    validate_whole_task("AssetHostTasksV1", source_tasks)
    host_preregistration(tasks, freeze)
    old, new = copy.deepcopy(source_tasks), copy.deepcopy(tasks)
    for value in (old, new):
        value.pop("freeze_digest", None)
        for task in value["tasks"]:
            task.pop("budgets_digest", None)
    if old != new:
        raise AutomatedHostRejected("source_tasks_changed")
    return protocol
