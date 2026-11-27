"""Test-suite guardrails (RM16; docs/TESTING.md): bounded tests and visible skips.

The project runs the deterministic suite in CI and locally while AI agents edit
it. A test timeout, registered markers, and a skip summary keep that run a
useful signal instead of an unbounded or silently smaller one.
"""


def test_the_suite_has_a_per_test_timeout(pytestconfig):
    """A hung test fails; it never holds a worker or a job indefinitely."""
    assert float(pytestconfig.getini("timeout")) == 900
    # The thread method leaves SIGALRM for the qualification tests that set
    # their own alarm-based deadlines.
    assert pytestconfig.getini("timeout_method") == "thread"


def test_the_suite_uses_strict_markers(pytestconfig):
    """A misspelled marker is a collection error, not a silently inert test."""
    assert pytestconfig.getoption("strict_markers") is True


def test_the_slow_marker_is_registered(pytestconfig):
    """Fast local runs can leave the three longest tests out explicitly."""
    assert any(line.startswith("slow:") for line in pytestconfig.getini("markers"))
