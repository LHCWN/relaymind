"""The asset-router interop harness itself (RM47, AC42): default suite, no router needed."""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from helpers import run_script
from router_interop import EmbeddingDouble, router_revision

ROOT = Path(__file__).resolve().parent.parent


def test_the_runner_names_an_exact_router_commit_never_a_branch():
    for rev in ("dev", "HEAD", "c3cc835"):
        result = run_script("asset_router_interop", "--rev", rev)
        assert result.returncode == 2
        assert "full 40-hex commit" in result.stderr


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_a_router_checkout_with_local_changes_is_refused(tmp_path):
    git("init", "-q", cwd=tmp_path)
    (tmp_path / "a.txt").write_text("one")
    git("add", "a.txt", cwd=tmp_path)
    git("-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-qm", "c", cwd=tmp_path)
    head = router_revision(tmp_path)
    assert len(head) == 40
    (tmp_path / "a.txt").write_text("two")
    with pytest.raises(RuntimeError, match="local changes"):
        router_revision(tmp_path)


def test_the_embedding_double_is_deterministic_and_can_fail_on_purpose():
    one, two = EmbeddingDouble(), EmbeddingDouble()
    assert one.vector("merge pdf files") == two.vector("merge pdf files")
    assert one.vector("merge pdf files") != one.vector("weather today")
    assert EmbeddingDouble(seed="v2").vector("merge pdf files") != one.vector("merge pdf files")
    double = EmbeddingDouble(fail_marker="boom").start()
    try:
        ok = httpx.post(double.url + "/v1/embeddings", json={"model": "m", "input": "fine"})
        failed = httpx.post(double.url + "/v1/embeddings", json={"model": "m", "input": "a boom"})
    finally:
        double.stop()
    assert ok.status_code == 200 and len(ok.json()["data"][0]["embedding"]) == 64
    assert failed.status_code == 500


def test_the_interop_module_skips_without_an_explicit_checkout():
    env = {k: v for k, v in os.environ.items() if not k.startswith("RELAYMIND_ASSET_ROUTER")}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "interop",
            "-p",
            "no:cacheprovider",
            "-rs",
            "tests/test_guidance_router_interop.py",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipped" in result.stdout and "RELAYMIND_ASSET_ROUTER_DIR" in result.stdout
