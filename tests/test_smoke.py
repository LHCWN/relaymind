"""Package import smoke test.

Verifies the top-level package imports and exposes its version. Feature and
acceptance tests reference requirement IDs from docs/SPECIFICATION.md
(RM01-RM47, AC01-AC42; RM30 and AC23 retired).
"""

import relaymind


def test_package_imports_and_has_version():
    assert relaymind.__version__ == "0.1.0"
