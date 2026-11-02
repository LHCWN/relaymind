"""RM12/RM16/RM44: runtime excerpts remain private in the automated bundle."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_originals_use_private_resolver_and_public_report_has_no_path(tmp_path):
    path = ROOT / "evaluation/assets/automated-host-277/bundle.py"
    spec = importlib.util.spec_from_file_location("automated_bundle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = module.private_runtime_bundle(module.legacy.Bundle, {"raw-call"})
    bundle = cls(tmp_path / "public", tmp_path / "private")
    bundle.file(
        "raw-call",
        "original",
        "redistributable",
        b"private source excerpt",
        publish=True,
        relative="raw/call.json",
    )
    assert not (tmp_path / "public/raw/call.json").exists()
    assert (tmp_path / "private/raw/call.json").read_bytes() == b"private source excerpt"
    assert bundle.evidence["raw-call"]["access"] == "restricted"
    assert bundle.evidence["raw-call"]["path"] is None
    report = {"evidence": [{"id": "raw-call", "path": "raw/call.json"}]}
    bundle.write(tmp_path / "public/report.json", report)
    assert report["evidence"][0]["path"] is None
    assert bundle.resolver[0]["path"] == "raw/call.json"


def test_bundle_refuses_overlapping_roots_before_writing(tmp_path):
    import pytest

    path = ROOT / "evaluation/assets/automated-host-277/bundle.py"
    spec = importlib.util.spec_from_file_location("automated_bundle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = module.private_runtime_bundle(module.legacy.Bundle, {"raw"})
    for out, private in (
        (tmp_path / "same", tmp_path / "same"),
        (tmp_path / "out", tmp_path / "out/private"),
        (tmp_path / "parent/out", tmp_path / "parent"),
    ):
        with pytest.raises(ValueError):
            cls(out, private)
        assert not out.exists()
        assert not private.exists()
