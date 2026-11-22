#!/usr/bin/env python3
"""Check guidance selector sync against the real asset-router at one exact commit (RM47, AC42).

  python scripts/asset_router_interop.py --rev <40-hex commit> [--repo URL|PATH] [--result PATH]

A run passes only when every interop test ran and passed; a skipped or empty run is
`not_validated`. Details: docs/TESTING.md, "Interop with asset-router". Development tooling only.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from relaymind.evaluation.checkpoint import atomic_json

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "git@github.com:LHCWN/asset-router.git"
DEFAULT_CACHE = Path.home() / ".cache" / "relaymind-interop"
COMMIT = re.compile(r"^[0-9a-f]{40}$")


def run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        sys.exit(f"{' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def prepare(repo: str, rev: str, cache: Path) -> Path:
    """A clean checkout of `rev` with its locked dependencies installed."""
    checkout = cache / f"asset-router-{rev}"
    if not (checkout / ".git").exists():
        cache.mkdir(parents=True, exist_ok=True)
        run("git", "clone", "--quiet", "--no-checkout", repo, str(checkout))
    if subprocess.run(["git", "cat-file", "-e", f"{rev}^{{commit}}"], cwd=checkout).returncode:
        run("git", "fetch", "--quiet", "origin", rev, cwd=checkout)
    run(
        "git",
        "-c",
        "advice.detachedHead=false",
        "checkout",
        "--quiet",
        "--detach",
        rev,
        cwd=checkout,
    )
    if not (checkout / "node_modules" / "tsx" / "dist" / "loader.mjs").is_file():
        npm = shutil.which("npm")
        if npm is None:
            sys.exit("npm is required to install asset-router dependencies")
        subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=checkout, check=True)
    return checkout


def relaymind_state() -> tuple[str, bool]:
    """This checkout's commit, and whether its tracked files match that commit.

    Not environment.application_revision: that one also counts untracked files as changes and
    returns None when git fails, while a result must name its commit, so this one fails loudly."""
    head = run("git", "rev-parse", "HEAD", cwd=ROOT)
    return head, not run("git", "status", "--porcelain", "--untracked-files=no", cwd=ROOT)


def run_pytest(command: list[str], env: dict[str, str]) -> int:
    return subprocess.run(command, cwd=ROOT, env=env).returncode


def counts(report: Path) -> dict[str, int]:
    """Totals of a pytest JUnit report; a missing or unreadable one counts no test."""
    totals = dict(tests=0, failed=0, skipped=0)
    try:
        suites = ElementTree.parse(report).getroot().iter("testsuite")
    except (OSError, ElementTree.ParseError):
        return totals
    for suite in suites:
        totals["tests"] += int(suite.get("tests", 0))
        totals["failed"] += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
        totals["skipped"] += int(suite.get("skipped", 0))
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rev", required=True, help="exact 40-hex asset-router commit")
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help="private router repository: authenticated SSH (default), HTTPS or a local clone",
    )
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--result", type=Path, help="write the outcome, counts and both commits here as JSON"
    )
    parser.add_argument("pytest_args", nargs="*", help="extra pytest arguments, after --")
    args = parser.parse_args()
    if not COMMIT.match(args.rev):
        parser.error("--rev must be a full 40-hex commit, never a branch name")
    relaymind, clean = relaymind_state()
    checkout = prepare(args.repo, args.rev, args.cache)
    print(f"asset-router {args.rev} at {checkout}", file=sys.stderr)
    env = {
        **os.environ,
        "RELAYMIND_ASSET_ROUTER_DIR": str(checkout),
        "RELAYMIND_ASSET_ROUTER_REV": args.rev,
    }
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "interop.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "interop",
            "-rA",
            str(ROOT / "tests" / "test_guidance_router_interop.py"),
            *args.pytest_args,
            f"--junitxml={report}",
        ]
        code = run_pytest(command, env)
        totals = counts(report)
    # A skipped or empty run says nothing about the router: it is not a pass.
    if code == 0 and totals["tests"] and not totals["skipped"] and not totals["failed"]:
        outcome = "passed"
    elif totals["failed"]:
        outcome = "failed"
    else:
        outcome = "not_validated"
    passed = totals["tests"] - totals["failed"] - totals["skipped"]
    result = dict(
        schema_version=1,
        outcome=outcome,
        relaymind_commit=relaymind,
        relaymind_clean=clean,
        router_commit=args.rev,
        tests=totals["tests"],
        passed=passed,
        failed=totals["failed"],
        skipped=totals["skipped"],
    )
    if args.result:
        atomic_json(args.result, result)
    dirty = "" if clean else " (with local changes)"
    print(
        f"interop {outcome.replace('_', ' ')}: {passed} passed, {totals['failed']} failed,"
        f" {totals['skipped']} skipped of {totals['tests']}; RelayMind {relaymind}{dirty},"
        f" asset-router {args.rev}",
        file=sys.stderr,
    )
    return 0 if outcome == "passed" else code or 1


if __name__ == "__main__":
    sys.exit(main())
