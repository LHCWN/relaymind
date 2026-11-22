"""The asset-router interop harness itself (RM47, AC42): default suite, no router needed."""

import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

import httpx
import pytest
import yaml

from helpers import load_script_module
from router_interop import (
    DIMENSIONS,
    ROUTER_DIR_ENV,
    ROUTER_REQUIRED_ENV,
    EmbeddingDouble,
    require_checkout,
    router_revision,
)

runner = load_script_module("asset_router_interop")
WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/router-interop.yml"


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


def test_the_interop_harness_uses_the_installed_loader_without_shell_shims(monkeypatch, tmp_path):
    """RM47, AC42: npm's platform-specific executables are optional for the harness."""
    loader = tmp_path / "node_modules" / "tsx" / "dist" / "loader.mjs"
    loader.parent.mkdir(parents=True)
    loader.touch()
    monkeypatch.setenv(ROUTER_DIR_ENV, str(tmp_path))
    try:
        checkout = require_checkout()
    except pytest.skip.Exception as error:
        pytest.fail(str(error))
    assert checkout == tmp_path.resolve()


def test_a_required_router_checkout_fails_instead_of_skipping(monkeypatch, tmp_path):
    """RM47, AC42: the runner and CI set the required mode, so a skip cannot pass."""
    monkeypatch.setenv(ROUTER_REQUIRED_ENV, "1")
    monkeypatch.delenv(ROUTER_DIR_ENV, raising=False)
    with pytest.raises(pytest.fail.Exception, match=ROUTER_DIR_ENV):
        require_checkout()
    monkeypatch.setenv(ROUTER_DIR_ENV, str(tmp_path))
    with pytest.raises(pytest.fail.Exception, match="npm ci"):
        require_checkout()


def test_the_runner_requires_the_checkout_it_prepared(monkeypatch, tmp_path):
    rev = "a" * 40
    seen = {}
    monkeypatch.setattr(runner, "prepare", lambda repo, rev, cache: tmp_path)
    monkeypatch.setattr(
        runner.subprocess, "run", lambda command, cwd, env: seen.update(env) or Completed()
    )
    monkeypatch.setattr(sys, "argv", ["asset_router_interop.py", "--rev", rev])
    assert runner.main() == 0
    assert seen[ROUTER_REQUIRED_ENV] == "1"
    assert seen[ROUTER_DIR_ENV] == str(tmp_path)


class Completed:
    returncode = 0


def test_the_interop_workflow_runs_the_runner_at_a_pinned_commit():
    """RM47, AC42: CI names one exact router commit and never a branch."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{40}", workflow["env"]["ROUTER_REV"])
    (job,) = workflow["jobs"].values()
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "scripts/asset_router_interop.py" in commands
    assert '--rev "$REV"' in commands
    triggers = workflow[True]  # YAML 1.1 reads the bare key "on" as true.
    for path in (
        ".github/workflows/router-interop.yml",
        "src/relaymind/guidance/sync.py",
        "tests/router_interop.py",
        "tests/test_guidance_router_interop.py",
        "scripts/asset_router_interop.py",
    ):
        assert any(fnmatch(path, pattern) for pattern in triggers["pull_request"]["paths"]), path
