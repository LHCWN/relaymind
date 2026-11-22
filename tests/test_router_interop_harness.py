"""The asset-router interop harness itself (RM47, AC42): default suite, no router needed."""

import subprocess
import sys

import httpx
import pytest

from helpers import load_script_module
from router_interop import (
    DIMENSIONS,
    ROUTER_DIR_ENV,
    EmbeddingDouble,
    require_checkout,
    router_revision,
)

runner = load_script_module("asset_router_interop")


@pytest.mark.parametrize("rev", ["dev", "HEAD", "c3cc835"])
def test_the_runner_names_an_exact_router_commit_never_a_branch(rev, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["asset_router_interop.py", "--rev", rev])
    with pytest.raises(SystemExit) as exit_:
        runner.main()
    assert exit_.value.code == 2
    assert "full 40-hex commit" in capsys.readouterr().err


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
    assert ok.status_code == 200 and len(ok.json()["data"][0]["embedding"]) == DIMENSIONS
    assert failed.status_code == 500


def test_the_interop_tests_skip_without_an_explicit_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv(ROUTER_DIR_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception, match=ROUTER_DIR_ENV):
        require_checkout()
    monkeypatch.setenv(ROUTER_DIR_ENV, str(tmp_path))
    with pytest.raises(pytest.skip.Exception, match="npm ci"):
        require_checkout()
