"""Frozen machine judging for the automated experiment (RM16/RM44; AC36).

Reuses the existing blind map, ledger, two-judge checks and adjudication.
Only this process selects the new frozen model; historical defaults stay intact.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))

from relaymind.canonical import canonical_digest, canonical_json  # noqa: E402
from relaymind.evaluation.asset_automated_host import validate_protocol  # noqa: E402
from relaymind.evaluation.asset_corpus_io import parse_json  # noqa: E402
from relaymind.evaluation.asset_run_journal import _fsync_directory  # noqa: E402
from relaymind.recommendations.private_files import create_private_file_exclusive  # noqa: E402


def read(path: Path) -> dict:
    return parse_json(path.read_bytes())


def judging_identity(protocol: dict, arguments: list[str]) -> dict | None:
    """Bind the selected judge to its frozen role, model and exact template."""
    protocol = validate_protocol(protocol)
    names = ("judge-a", "judge-b", "judge-adjudicator")
    for entry, name in zip(protocol["judges"], names, strict=True):
        if (
            entry["id"] != name
            or entry["prompt_sha256"]
            != hashlib.sha256((HERE / "prompts" / (name + ".txt")).read_bytes()).hexdigest()
        ):
            raise ValueError("judge_binding")
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "command", choices=("blind", "bind-blinding", "judge", "adjudicate", "apply")
    )
    parser.add_argument("--template")
    parser.add_argument("--name")
    parsed, _ = parser.parse_known_args(arguments)
    if parsed.command == "judge":
        if parsed.name not in names[:2] or parsed.template != parsed.name + ".txt":
            raise ValueError("judge_binding")
        return protocol["judges"][names.index(parsed.name)]
    if parsed.command == "adjudicate":
        return protocol["judges"][2]
    return None


def command_home(command: str) -> Path:
    """Original tasks retain their original rubrics; only judging uses new prompts."""
    return HERE if command in ("judge", "adjudicate") else HERE.parent / "owner-run-260"


def blinding_manifest(items_path: Path, map_digest_path: Path) -> dict:
    items = read(items_path)
    digest = read(map_digest_path)
    if digest.get("format") != "relaymind.owner_run_blind_map_digest.v1" or digest.get(
        "items"
    ) != len(items.get("items", [])):
        raise ValueError("blinding_map_digest")
    result = dict(
        schema_version=1,
        profile="automated_host",
        items_sha256=hashlib.sha256(items_path.read_bytes()).hexdigest(),
        items_bytes=items_path.stat().st_size,
        map_digest_sha256=hashlib.sha256(map_digest_path.read_bytes()).hexdigest(),
        map_digest_bytes=map_digest_path.stat().st_size,
        map_canonical_sha256=digest.get("map_canonical_sha256"),
        items_count=len(items["items"]),
    )
    validate_blinding(result, items_path)
    return result


def validate_blinding(
    manifest: dict, items_path: Path | None = None, *, mapping: dict | None = None
) -> None:
    """Validate committed item bytes without opening the private map for a judge."""
    if (
        set(manifest)
        != {
            "schema_version",
            "profile",
            "items_sha256",
            "items_bytes",
            "map_digest_sha256",
            "map_digest_bytes",
            "map_canonical_sha256",
            "items_count",
        }
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["profile"] != "automated_host"
    ):
        raise ValueError("blinding_manifest")
    for key in ("items_sha256", "map_digest_sha256", "map_canonical_sha256"):
        if not isinstance(manifest[key], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest[key]):
            raise ValueError("blinding_manifest")
    for key in ("items_bytes", "map_digest_bytes", "items_count"):
        if type(manifest[key]) is not int or manifest[key] < 0:
            raise ValueError("blinding_manifest")
    if items_path is not None:
        payload = items_path.read_bytes()
        if (
            len(payload) != manifest["items_bytes"]
            or hashlib.sha256(payload).hexdigest() != manifest["items_sha256"]
        ):
            raise ValueError("blinding_items")
        items = parse_json(payload)
        if (
            items.get("format") != "relaymind.owner_run_blind_items.v1"
            or not isinstance(items.get("items"), list)
            or len(items["items"]) != manifest["items_count"]
        ):
            raise ValueError("blinding_items")
        ids = [item.get("blind_id") for item in items["items"] if isinstance(item, dict)]
        if (
            len(ids) != len(items["items"])
            or any(not isinstance(i, str) or not i for i in ids)
            or len(ids) != len(set(ids))
        ):
            raise ValueError("blinding_items")
    if mapping is not None and canonical_digest(mapping) != manifest["map_canonical_sha256"]:
        raise ValueError("blinding_map")


def require_blinding(
    check_committed,
    commit: str | None,
    manifest: Path,
    items: Path | None,
    *,
    mapping: dict | None = None,
) -> None:
    if commit is None:
        raise ValueError("blinding_commit_required")
    check_committed(commit, [manifest])
    validate_blinding(read(manifest), items, mapping=mapping)


def _durable(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".judging-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((canonical_json(value) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _captured_answer(invocation: Path, request_sha256: str) -> dict:
    """Recover a completed durable response after process/output interruption."""
    complete = invocation / "transport.json"
    if complete.is_file():
        metadata = read(complete)
        if metadata.get("request_sha256") != request_sha256:
            raise ValueError("judge_capture_changed")
        raw = (invocation / "response.bin").read_bytes()
        if hashlib.sha256(raw).hexdigest() != metadata.get("response_sha256") or len(
            raw
        ) != metadata.get("response_bytes"):
            raise ValueError("judge_capture_changed")
    else:
        folders = list((invocation / "originals").glob("*"))
        if len(folders) != 1 or not folders[0].is_dir() or folders[0].is_symlink():
            raise ValueError("judge_invocation_incomplete")
        try:
            metadata = read(folders[0] / "result.json")
            if (
                hashlib.sha256((folders[0] / "request.bin").read_bytes()).hexdigest()
                != request_sha256
            ):
                raise ValueError("judge_capture_changed")
            raw = (folders[0] / "response.bin").read_bytes()
        except (OSError, ValueError):
            raise ValueError("judge_invocation_incomplete") from None
    if metadata.get("outcome") != "completed" or not 200 <= metadata.get("status", 0) < 300:
        raise ValueError("judge_invocation_incomplete")
    return parse_json(raw)


def _settle_answer(ledger, entry, answer):
    usage = answer.get("usage")
    if not isinstance(usage, dict) or any(
        type(usage.get(key)) is not int or usage[key] < 0
        for key in ("prompt_tokens", "completion_tokens")
    ):
        ledger.unknown(entry)
    else:
        ledger.settle(entry, usage)
    return answer


def bounded_judge_chat(model, temperature, request_bound, transport):
    """Recover identical invocations; never replay a possibly dispatched call."""

    def chat(ledger, stage, messages, log_dir, name, *, max_output=4096):
        payload = json.dumps(
            dict(model=model, temperature=temperature, messages=messages, max_tokens=max_output)
        ).encode()
        identity = dict(stage=stage, name=name, request_sha256=hashlib.sha256(payload).hexdigest())
        invocation = Path(ledger.path).with_name(
            Path(ledger.path).name + ".invocations"
        ) / canonical_digest(identity)
        invocation.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            invocation.mkdir(mode=0o700)
            fresh = True
        except FileExistsError:
            if invocation.is_symlink():
                raise ValueError("judge_invocation_incomplete") from None
            fresh = False
        marker = invocation / "invocation.json"
        if not fresh:
            try:
                recorded = read(marker)
                if (
                    any(recorded.get(key) != value for key, value in identity.items())
                    or recorded.get("entry") is None
                ):
                    raise ValueError("judge_invocation_incomplete")
                entry = recorded["entry"]
                answer = _captured_answer(invocation, identity["request_sha256"])
            except (OSError, ValueError):
                raise ValueError("judge_invocation_incomplete") from None
            return _settle_answer(ledger, entry, answer)
        _durable(marker, dict(identity, entry=None))
        entry = ledger.reserve(stage, name, request_bound(payload, max_output))
        _durable(marker, dict(identity, entry=entry))
        try:
            result = transport(
                os.environ["RELAYMIND_EXTERNAL_BASE_URL"].rstrip("/") + "/chat/completions",
                payload,
                {
                    "Authorization": "Bearer " + os.environ["RELAYMIND_EXTERNAL_API_KEY"],
                    "Content-Type": "application/json",
                },
                120,
                capture_root=invocation / "originals",
            )
            raw = result["body"]
            if not create_private_file_exclusive(invocation / "response.bin", raw):
                raise ValueError("judge_capture_exists")
            _durable(
                invocation / "transport.json",
                dict(
                    status=result["status"],
                    outcome=result["outcome"],
                    request_sha256=identity["request_sha256"],
                    response_sha256=hashlib.sha256(raw).hexdigest(),
                    response_bytes=len(raw),
                ),
            )
            answer = _captured_answer(invocation, identity["request_sha256"])
        except Exception:
            ledger.unknown(entry)
            raise
        return _settle_answer(ledger, entry, answer)

    return chat


@contextmanager
def ledger_lock(path: Path):
    """Serialize judges sharing one cap; kernel locks release after a crash."""
    path = path.with_name(path.name + ".judging.lock")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    create_private_file_exclusive(path, b"0")
    if path.is_symlink():
        raise ValueError("judge_ledger_lock")
    with path.open("r+b") as stream:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("judge_ledger_locked") from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("judge_ledger_locked") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--preregistration-commit", required=True)
    parser.add_argument("--blind-manifest", type=Path, required=True)
    parser.add_argument("--blinding-commit")
    args, rest = parser.parse_known_args()
    entry = judging_identity(read(args.protocol), rest)
    inspect = argparse.ArgumentParser(add_help=False)
    inspect.add_argument("command")
    for name in ("items", "map", "map-digest", "ledger"):
        inspect.add_argument("--" + name, type=Path)
    inspect.add_argument("--cap", type=int)
    inspect.add_argument("--batch", type=int, default=6)
    selected, _ = inspect.parse_known_args(rest)
    if entry is not None:
        if (
            selected.cap != read(HERE / "configs/budget.json")["judge_token_limit"]
            or selected.batch != 6
        ):
            raise ValueError("judge_budget_binding")
        if selected.ledger is None or selected.items is None:
            raise ValueError("judge_inputs")
    run_spec = importlib.util.spec_from_file_location("automated_277_run", HERE / "run.py")
    run = importlib.util.module_from_spec(run_spec)
    run_spec.loader.exec_module(run)
    run.require_committed_inputs(
        args.preregistration_commit,
        [
            args.protocol,
            Path(__file__),
            *sorted((HERE / "prompts").glob("judge-*.txt")),
            ROOT / "scripts/automated_host_transport.py",
            HERE / "configs/budget.json",
            HERE / "run.py",
            HERE.parent / "owner-run-260/judge.py",
            HERE.parent / "owner-run-260/harness.py",
        ],
    )
    if selected.command in ("judge", "adjudicate", "apply"):
        require_blinding(
            run.require_committed_inputs,
            args.blinding_commit,
            args.blind_manifest,
            selected.items,
            mapping=read(selected.map) if selected.command == "apply" else None,
        )
    elif args.blind_manifest.exists():
        raise ValueError("blinding_output_exists")
    if selected.command == "bind-blinding":
        value = blinding_manifest(selected.items, selected.map_digest)
        if not create_private_file_exclusive(
            args.blind_manifest, (canonical_json(value) + "\n").encode()
        ):
            raise ValueError("blinding_output_exists")
        return 0
    path = HERE.parent / "owner-run-260/judge.py"
    spec = importlib.util.spec_from_file_location("automated_277_judging", path)
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    legacy.HERE = command_home(selected.command)
    legacy.write = lambda path, value: _durable(Path(path), value)
    if entry is not None:
        from automated_host_transport import bounded_json_request

        legacy.harness.MODEL = entry["model"]
        legacy.harness.SAMPLING = {"temperature": entry["temperature"]}
        legacy.harness.chat = bounded_judge_chat(
            entry["model"], entry["temperature"], legacy.harness.request_bound, bounded_json_request
        )
    sys.argv = [str(Path(__file__)), *rest]
    if entry is not None:
        with ledger_lock(selected.ledger):
            legacy.main()
    else:
        if selected.command == "blind" and any(
            path is None or path.exists()
            for path in (selected.items, selected.map, selected.map_digest)
        ):
            raise ValueError("blinding_output_exists")
        legacy.main()
    if selected.command == "blind":
        value = blinding_manifest(selected.items, selected.map_digest)
        if not create_private_file_exclusive(
            args.blind_manifest, (canonical_json(value) + "\n").encode()
        ):
            raise ValueError("blinding_output_exists")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
