"""Shared pytest setup.

pyproject.toml is a reviewed build anchor (src/relaymind/evaluation/asset_evidence.py), so markers
that need no change to it are registered here.
"""


def pytest_configure(config):
    # RM47, AC42: runs the real asset-router from an explicit checkout; skipped without one.
    config.addinivalue_line(
        "markers",
        "interop: runs the real asset-router from a checkout named by RELAYMIND_ASSET_ROUTER_DIR",
    )
