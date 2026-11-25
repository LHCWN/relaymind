"""Test-only fixture benchmark authoring helper moved out of production code (#296)."""

import json
import tomllib
import unicodedata
from pathlib import Path

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_benchmark import (
    _effective_config,
    _load_benchmark_release,
    benchmark_inventories,
    load_production_config,
)
from relaymind.evaluation.asset_benchmark_io import (
    CONFIG_BYTES,
    atomic_bytes,
    json_bytes,
    validate_benchmark_document,
)
from relaymind.evaluation.asset_corpus_io import AssetCorpusError, document, read_local
from relaymind.evaluation.asset_evidence import collect_benchmark_environment


def refresh_fixture_benchmark_config(path: Path, *, reauthor_fixture_unicode: bool = False) -> None:
    """Explicitly author fresh fixture settings/environment on the current device.

    This authoring helper is never called by run or compare. It refuses genuine
    corpus purposes and never changes source data, labels, rights or policy.
    """
    path = Path(path).absolute()
    raw = tomllib.loads(read_local(path.parent, path.name, CONFIG_BYTES).decode("utf-8"))
    validate_benchmark_document("config", raw)
    lock_path = path.parent / raw["lock"]
    if type(reauthor_fixture_unicode) is not bool:
        raise AssetCorpusError("invalid_fixture_authoring")
    if reauthor_fixture_unicode:
        lock = document(lock_path.parent, lock_path.name, "lock")
        committed = {key: value for key, value in lock.items() if key != "digest"}
        if canonical_digest(committed) != lock["digest"]:
            raise AssetCorpusError("lock_digest")
        if lock["purpose"] != "fixture" or lock["state"] != "provisional" or lock["related_locks"]:
            raise AssetCorpusError("fixture_authoring_only")
        if lock["unicode_version"] != unicodedata.unidata_version:
            committed["unicode_version"] = unicodedata.unidata_version
            authored = committed | dict(digest=canonical_digest(committed))
            atomic_bytes(lock_path, json_bytes(authored), limit=CONFIG_BYTES)
    release = _load_benchmark_release(lock_path)
    if release.lock["purpose"] != "fixture":
        raise AssetCorpusError("fixture_authoring_only")
    read_local(path.parent, raw["production_config"], CONFIG_BYTES)
    production = load_production_config(path.parent / raw["production_config"])
    inventories = benchmark_inventories()
    effective = _effective_config(production, release, inventories, raw["split"])
    environment = collect_benchmark_environment()
    raw.update(
        lock_digest=release.lock["digest"],
        production_config_digest=canonical_digest(effective),
        environment_lock_digest=canonical_digest(environment),
    )
    atomic_bytes(path.parent / raw["environment_lock"], json_bytes(environment), limit=CONFIG_BYTES)
    lines = [
        key + " = " + json.dumps(value, ensure_ascii=False)
        for key, value in raw.items()
        if key != "preparation"
    ]
    if "preparation" in raw:
        lines.append("[preparation]")
        lines.extend(
            key + " = " + json.dumps(value, ensure_ascii=False)
            for key, value in raw["preparation"].items()
            if value is not None
        )
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    atomic_bytes(path, payload, limit=CONFIG_BYTES)
