"""Internal automated experiment artifact bindings (RM16/RM44; spec 12.6.7).

This serialization is an assembly aid, not a new acceptance contract. It checks
file identity and report conservation, never whether reviewers approve, judging
is complete, claims are true, or the experiment satisfies acceptance. A fixture
or an incomplete run can have valid bindings. Actual review remains necessary.
All file paths, including private originals, are relative to the record file.
"""

import re
from collections.abc import Mapping
from pathlib import Path

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_automated_host import (
    AutomatedHostRejected,
    _closed,
    _hash,
    _id,
    bind_inputs,
)
from relaymind.evaluation.asset_corpus_io import DOCUMENT_BYTES, parse_json, read_local
from relaymind.evaluation.asset_qualification_record import _read_bound, _time
from relaymind.evaluation.asset_whole_task import grade_host_report

FILE_BYTES = 16 * 1024 * 1024


def _reject() -> None:
    raise AutomatedHostRejected("automated_record_binding")


def _file(root: Path, ref: Mapping) -> bytes:
    _closed(ref, "path sha256 bytes")
    _hash(ref["sha256"])
    if type(ref["bytes"]) is not int or not 0 <= ref["bytes"] <= FILE_BYTES:
        _reject()
    return _read_bound(root, ref, FILE_BYTES)


def _manifest(root: Path, value: Mapping, *, identified: bool = False) -> list:
    _closed(value, "files")
    rows = value["files"]
    if not isinstance(rows, list):
        _reject()
    seen = set()
    for row in rows:
        _closed(row, "id path sha256 bytes" if identified else "path sha256 bytes")
        key = row["id"] if identified else row["path"]
        if identified:
            _id(key)
        if key in seen:
            _reject()
        seen.add(key)
        _file(root, {k: v for k, v in row.items() if k != "id"})
    return rows


def load_automated_record(path: Path) -> dict:
    """Verify declared bindings; return no approval or completion decision.

    The closed record has protocol/source_tasks/source_lock/tasks/freeze/report,
    input_manifest/evidence_manifest/code_manifest file references; preregistration
    (commit, committed_at); first_dispatch (at, capture); judging references
    (blind_manifest, blind_map, judgments, usage, captures); reviews; limitations.
    A reference is {path, sha256, bytes}. Evidence/code/judging capture manifests
    contain {files:[reference,...]}; evidence entries additionally carry `id`.
    Reviews contain id/model/context_id/started_at/completed_at and
    prompt/input/output/usage references. Their input is {schema_version:1,
    claim_sha256:canonical_digest(record without reviews)}. Actual output text is
    retained and checked for presence; its verdict is deliberately not inferred.
    """
    root, name = Path(path).absolute().parent, Path(path).name
    record = parse_json(read_local(root, name, DOCUMENT_BYTES))
    _closed(
        record,
        "schema_version profile protocol source_tasks source_lock tasks freeze report "
        "input_manifest evidence_manifest code_manifest preregistration first_dispatch "
        "judging reviews limitations",
    )
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        _reject()
    if record["profile"] != "automated_host":
        _reject()
    keys = (
        "protocol",
        "source_tasks",
        "source_lock",
        "tasks",
        "freeze",
        "report",
        "input_manifest",
        "evidence_manifest",
        "code_manifest",
    )
    values = {key: parse_json(_file(root, record[key])) for key in keys}
    protocol = bind_inputs(
        **{
            key: values[key]
            for key in (
                "protocol",
                "source_tasks",
                "source_lock",
                "tasks",
                "freeze",
            )
        }
    )
    report = values["report"]
    grade_host_report(report, values["tasks"], values["freeze"])
    input_ref = report["input_manifest"]
    if (
        input_ref["digest"] != canonical_digest(values["input_manifest"])
        or input_ref["bytes"] != record["input_manifest"]["bytes"]
    ):
        _reject()
    evidence = _manifest(root, values["evidence_manifest"], identified=True)

    def projected(rows):
        return {r["id"]: (r["sha256"], r["bytes"]) for r in rows}

    if projected(evidence) != projected(report["evidence"]):
        _reject()
    if not _manifest(root, values["code_manifest"]):
        _reject()
    prereg = record["preregistration"]
    _closed(prereg, "commit committed_at")
    if not isinstance(prereg["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", prereg["commit"]):
        _reject()
    dispatch = record["first_dispatch"]
    _closed(dispatch, "at capture")
    capture = parse_json(_file(root, dispatch["capture"]))
    if not isinstance(capture, Mapping) or capture.get("started_at") != dispatch["at"]:
        _reject()
    if _time(prereg["committed_at"]) > _time(dispatch["at"]):
        _reject()
    known_starts = [
        a["started_at"]
        for row in report["rows"]
        for a in row["attempts"]
        if a["started_at"] is not None
    ]
    if known_starts and _time(dispatch["at"]) > min(map(_time, known_starts)):
        _reject()
    judging = record["judging"]
    _closed(judging, "blind_manifest blind_map judgments usage captures")
    for key, ref in judging.items():
        payload = parse_json(_file(root, ref))
        if not isinstance(payload, Mapping):
            _reject()
        if key == "captures":
            _manifest(root, payload)
    limitations = record["limitations"]
    if not isinstance(limitations, list) or not limitations:
        _reject()
    for limitation in limitations:
        if not isinstance(limitation, str) or not limitation.strip():
            _reject()
    reviews = record["reviews"]
    if not isinstance(reviews, list):
        _reject()
    expected = {row["id"]: row for row in protocol["reviewers"]}
    if len(reviews) != len(expected):
        _reject()
    claim = canonical_digest({k: v for k, v in record.items() if k != "reviews"})
    ids, contexts = set(), set()
    for review in reviews:
        _closed(review, "id model context_id started_at completed_at prompt input output usage")
        _id(review["id"])
        _id(review["context_id"])
        if review["id"] not in expected or review["id"] in ids or review["context_id"] in contexts:
            _reject()
        ids.add(review["id"])
        contexts.add(review["context_id"])
        frozen = expected[review["id"]]
        if (
            review["model"] != frozen["model"]
            or review["prompt"]["sha256"] != frozen["prompt_sha256"]
        ):
            _reject()
        if (
            not _time(dispatch["at"])
            <= _time(review["started_at"])
            <= _time(review["completed_at"])
        ):
            _reject()
        payloads = {key: _file(root, review[key]) for key in ("prompt", "input", "output", "usage")}
        if any(not payloads[key].decode("utf-8").strip() for key in ("prompt", "output")):
            _reject()
        if parse_json(payloads["input"]) != {"schema_version": 1, "claim_sha256": claim}:
            _reject()
        if not isinstance(parse_json(payloads["usage"]), Mapping):
            _reject()
    return dict(
        bindings_verified=True,
        completion_verified=False,
        planned_rows=len(report["planned"]),
        qualification=report["qualification"],
        record_digest=canonical_digest(record),
    )
