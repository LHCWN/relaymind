"""Architecture import-contract and component-map checks (#297).

Enforces the layering rules and named subsystem boundaries documented in
docs/ARCHITECTURE.md so accidental upward imports or undocumented cross-layer
edges fail deterministically in CI.
"""

import ast
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "relaymind"
ARCH_DOC = REPO / "docs" / "ARCHITECTURE.md"

LEAVES = frozenset({"canonical", "schema_util", "redaction"})
CORE_STORE_MODULES = frozenset({"run_store.py", "trace_event.py", "model_timings.py"})

# Documented package-level allowed dependencies from docs/ARCHITECTURE.md.
ALLOWED_PACKAGE_EDGES: dict[str, frozenset[str]] = {
    "canonical": frozenset(),
    "schema_util": frozenset(),
    "redaction": frozenset(),
    "adapters": frozenset(),
    "stats": frozenset({"adapters"}),
    "identifiers": frozenset({"adapters", "tools"}),
    "tools": frozenset({"adapters", "store"}) | LEAVES,
    "skills": frozenset({"tools", "identifiers"}) | LEAVES,
    "store": frozenset({"assets", "recommendations", "selection"}) | LEAVES,
    "orchestrator": frozenset({"adapters", "identifiers", "selection", "skills", "store", "tools"})
    | LEAVES,
    "selection": frozenset(
        {
            "adapters",
            "assets",
            "identifiers",
            "orchestrator",
            "recommendations",
            "skills",
            "store",
            "tools",
        }
    )
    | LEAVES,
    "assets": frozenset({"recommendations", "selection", "store", "tools"}) | LEAVES,
    "recommendations": frozenset({"api", "assets", "selection", "store"}) | LEAVES,
    "guidance": frozenset({"adapters", "assets", "recommendations", "selection"}) | LEAVES,
    "ui": frozenset({"adapters", "api", "orchestrator", "store"}),
    "api": frozenset(
        {
            "adapters",
            "assets",
            "guidance",
            "orchestrator",
            "recommendations",
            "selection",
            "store",
            "tools",
            "ui",
        }
    )
    | LEAVES,
    "openclaw_setup": frozenset({"evaluation"}),
    "launcher": frozenset(
        {
            "adapters",
            "api",
            "assets",
            "guidance",
            "openclaw_setup",
            "orchestrator",
            "recommendations",
            "selection",
            "skills",
            "store",
            "tools",
            "ui",
        }
    )
    | LEAVES,
    "evaluation": frozenset(
        {
            "adapters",
            "api",
            "assets",
            "launcher",
            "openclaw_setup",
            "orchestrator",
            "recommendations",
            "selection",
            "skills",
            "store",
            "tools",
        }
    )
    | LEAVES,
}


def _imported_modules(path: Path) -> set[str]:
    """Return every relaymind.* module path imported by a Python source file."""
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("relaymind."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "relaymind":
                for alias in node.names:
                    found.add(f"relaymind.{alias.name}")
            elif node.module.startswith("relaymind."):
                found.add(node.module)
    return found


def _package_of(path: Path) -> str:
    rel = path.relative_to(SRC)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def test_package_imports_match_documented_architecture_contract():
    """#297: every cross-package import obeys docs/ARCHITECTURE.md."""
    violations: list[str] = []
    discovered_packages: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "__init__.py" and path.parent == SRC:
            continue
        pkg = _package_of(path)
        discovered_packages.add(pkg)
        allowed = ALLOWED_PACKAGE_EDGES.get(pkg)
        if allowed is None:
            violations.append(f"undocumented package/module: {pkg} ({path.relative_to(REPO)})")
            continue
        for mod in sorted(_imported_modules(path)):
            target = mod.split(".")[1]
            if target == pkg:
                continue
            if target not in allowed:
                violations.append(f"{path.relative_to(REPO)}: {pkg} -> {target} ({mod})")
    assert not violations, "Forbidden cross-layer imports:\n" + "\n".join(violations)
    assert discovered_packages <= set(ALLOWED_PACKAGE_EDGES)


def test_core_store_modules_depend_only_on_leaves():
    """#297: core run_store/trace_event/model_timings never depend on adapters or higher layers."""
    violations: list[str] = []
    for name in sorted(CORE_STORE_MODULES):
        path = SRC / "store" / name
        for mod in sorted(_imported_modules(path)):
            target = mod.split(".")[1]
            if target != "store" and target not in LEAVES:
                violations.append(f"{path.relative_to(REPO)} imports {mod}")
    assert not violations, "Core store must only import leaves:\n" + "\n".join(violations)


def test_adapters_never_import_sqlite_or_other_relaymind_packages():
    """#297: adapters stay isolated from SQLite and other RelayMind packages."""
    violations: list[str] = []
    for path in sorted((SRC / "adapters").rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name == "sqlite3" or name.startswith("sqlite3."):
                    violations.append(f"{path.relative_to(REPO)} imports {name}")
                if name.startswith("relaymind.") and name.split(".")[1] != "adapters":
                    violations.append(f"{path.relative_to(REPO)} imports {name}")
    assert not violations, "Adapter isolation violations:\n" + "\n".join(violations)


def test_selection_never_creates_an_orchestrator_module_cycle():
    """#297: orchestrator modules imported by selection never import selection back."""
    imported_by_selection: set[str] = set()
    for path in sorted((SRC / "selection").rglob("*.py")):
        for mod in _imported_modules(path):
            parts = mod.split(".")
            if len(parts) >= 3 and parts[1] == "orchestrator":
                imported_by_selection.add(parts[2])
    back_edges: list[str] = []
    for mod_name in sorted(imported_by_selection):
        orch_file = SRC / "orchestrator" / f"{mod_name}.py"
        if orch_file.is_file():
            for mod in sorted(_imported_modules(orch_file)):
                if mod.split(".")[1] == "selection":
                    back_edges.append(f"{orch_file.relative_to(REPO)} imports {mod}")
    assert not back_edges, "Cycle between selection and orchestrator:\n" + "\n".join(back_edges)


def test_architecture_doc_lists_every_public_api_module():
    """#297: docs/ARCHITECTURE.md component map lists every major src/relaymind/api module."""
    text = ARCH_DOC.read_text("utf-8")
    api_modules = sorted(p.name for p in (SRC / "api").glob("*.py") if not p.name.startswith("_"))
    missing = [name for name in api_modules if f"`{name}`" not in text]
    assert not missing, f"API modules missing from docs/ARCHITECTURE.md: {missing}"


def test_skills_never_imports_upward_from_orchestrator():
    """#297: skills layer must not import orchestrator."""
    by_file: dict[str, list[str]] = defaultdict(list)
    for path in sorted((SRC / "skills").rglob("*.py")):
        for mod in sorted(_imported_modules(path)):
            if mod.split(".")[1] == "orchestrator":
                by_file[str(path.relative_to(REPO))].append(mod)
    assert not by_file, f"skills imports orchestrator: {dict(by_file)}"
