"""Benchmark data v3 re-pins the upstream rebuilt archives and changes nothing else (RM40; #284)."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "evaluation/assets/matrix-development"


def load(name):
    return json.loads((ROOT / name).read_text("utf-8"))


def test_v3_changes_only_the_pins_of_the_rebuilt_archives():
    old, new = load("sources.v2.json"), load("sources.v3.json")
    assert [a["asset_id"] for a in old["assets"]] == [a["asset_id"] for a in new["assets"]]
    assert old["exclusions"] == new["exclusions"]
    old_urls = {f["url"]: f for f in old["files"]}
    new_urls = {f["url"]: f for f in new["files"]}
    assert old_urls.keys() == new_urls.keys()
    changed = [u for u in old_urls if old_urls[u]["sha256"] != new_urls[u]["sha256"]]
    assert len(changed) == 96
    assert all("openharmony-matrix" in u for u in changed)
    for row in new["files"]:
        assert row["id"] == "file:" + row["sha256"]


def test_v3_can_be_built_with_every_asset_valid():
    assert all(
        isinstance(a["revision"], str) and a["revision"] for a in load("sources.v3.json")["assets"]
    )


def test_v3_keeps_every_case_and_every_label():
    old, new = load("cases.v2.json"), load("cases.v3.json")
    assert len(new["cases"]) == 128
    for before, after in zip(old["cases"], new["cases"], strict=True):
        for key in before:
            if key not in ("reference_evidence_ids", "source_evidence_ids"):
                assert before[key] == after[key]


def test_v3_moves_only_the_evidence_of_the_one_skill_whose_text_changed():
    old, new = load("cases.v2.json"), load("cases.v3.json")
    moved = {
        before["id"]
        for before, after in zip(old["cases"], new["cases"], strict=True)
        if before["reference_evidence_ids"] != after["reference_evidence_ids"]
        or before["source_evidence_ids"] != after["source_evidence_ids"]
    }
    assert moved == {"capability-09-en", "capability-09-zh"}


def test_v3_digest_is_self_consistent():
    from relaymind.canonical import canonical_digest

    new = load("cases.v3.json")
    assert new["digest"] == canonical_digest({k: v for k, v in new.items() if k != "digest"})
    assert new["catalog_digest"] != load("cases.v2.json")["catalog_digest"]


def test_v3_references_only_files_it_pins():
    recipe = load("sources.v3.json")
    files = {row["id"] for row in recipe["files"]}
    for asset in recipe["assets"]:
        for key in ("file_id", "metadata_file_id", "license_file_id"):
            assert asset.get(key) is None or asset[key] in files
    for row in recipe["evidence_files"]:
        assert row["file_id"] in files
