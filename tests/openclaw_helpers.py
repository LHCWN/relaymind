"""Shared OpenClaw Node dependency check helper for OpenClaw test modules (#296)."""

from collections.abc import Callable

import pytest


def require_node_dependencies(
    missing: Callable[[], str | None],
    *,
    required: bool,
    fail_prefix: str = "RELAYMIND_OPENCLAW_NODE=1 but ",
) -> None:
    """Skip or fail a test module when required OpenClaw Node dependencies are absent."""
    reason = missing()
    if reason is not None:
        if required:
            pytest.fail(f"{fail_prefix}{reason}")
        pytest.skip(reason)
