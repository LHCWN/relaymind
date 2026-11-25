"""Architecture import-contract and component-map checks (#297).

Enforces the layering rules and named subsystem boundaries documented in
docs/ARCHITECTURE.md so accidental upward imports or undocumented cross-layer
edges fail deterministically in CI.
"""

import ast
import re
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "relaymind"
ARCH_DOC = REPO / "docs" / "ARCHITECTURE.md"

LEAVES = frozenset({"canonical", "schema_util", "redaction"})
CORE_STORE_MODULES = frozenset(
    {"run_store.py", "trace_event.py", "model_timings.py", "outcomes.py"}
)
CORE_STORE_NAMES = frozenset(name.removesuffix(".py") for name in CORE_STORE_MODULES)

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


def _is_type_checking_if(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _walk_imports(node: ast.AST, *, include_type_checking: bool) -> list[ast.AST]:
    collected: list[ast.AST] = []
    stack: list[ast.AST] = [node]
    while stack:
        current = stack.pop()
        if not include_type_checking and _is_type_checking_if(current):
            # Walk orelse branch if present, skip the TYPE_CHECKING body.
            assert isinstance(current, ast.If)
            stack.extend(current.orelse)
            continue
        collected.append(current)
        stack.extend(ast.iter_child_nodes(current))
    return collected


def _imported_modules(
    path: Path, source: str | None = None, *, include_type_checking: bool = True
) -> set[str]:
    """Return every relaymind.* module path imported by a Python source file.

    Resolves relative imports against ``path`` and expands ``from relaymind.<pkg> import <name>``
    so package-level and relative imports cannot bypass architecture or cycle checks.
    """
    text = path.read_text("utf-8") if source is None else source
    tree = ast.parse(text, filename=str(path))
    rel = path.relative_to(SRC)
    pkg_parts = ["relaymind", *rel.parts[:-1]]
    found: set[str] = set()
    for node in _walk_imports(tree, include_type_checking=include_type_checking):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "relaymind" or alias.name.startswith("relaymind."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                trim = node.level - 1
                base = pkg_parts[: len(pkg_parts) - trim] if trim else list(pkg_parts)
                if not base or base[0] != "relaymind":
                    continue
                if node.module:
                    resolved = ".".join([*base, *node.module.split(".")])
                else:
                    resolved = ".".join(base)
            elif node.module and (
                node.module == "relaymind" or node.module.startswith("relaymind.")
            ):
                resolved = node.module
            else:
                continue

            if resolved == "relaymind":
                for alias in node.names:
                    if alias.name != "*":
                        found.add(f"relaymind.{alias.name}")
            elif len(resolved.split(".")) == 2 and all(alias.name != "*" for alias in node.names):
                for alias in node.names:
                    found.add(f"{resolved}.{alias.name}")
            else:
                found.add(resolved)
    return found


def _package_of(path: Path) -> str:
    rel = path.relative_to(SRC)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


# Documented narrow module-level restrictions on specific cross-package edges.
ALLOWED_MODULE_EDGES: dict[tuple[str, str], frozenset[str]] = {
    ("skills", "tools"): frozenset({"relaymind.tools.registry"}),
    ("recommendations", "api"): frozenset({"relaymind.api.errors"}),
    ("identifiers", "tools"): frozenset({"relaymind.tools.registry"}),
}


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
            parts = mod.split(".")
            if len(parts) < 2:
                continue
            target = parts[1]
            if target == pkg:
                continue
            if target not in allowed:
                violations.append(f"{path.relative_to(REPO)}: {pkg} -> {target} ({mod})")
                continue
            allowed_mods = ALLOWED_MODULE_EDGES.get((pkg, target))
            if allowed_mods is not None and mod not in allowed_mods:
                violations.append(
                    f"{path.relative_to(REPO)}: {pkg} -> {mod} (allowed: {sorted(allowed_mods)})"
                )
    assert not violations, "Forbidden cross-layer imports:\n" + "\n".join(violations)
    assert discovered_packages <= set(ALLOWED_PACKAGE_EDGES)


def test_core_store_modules_depend_only_on_leaves():
    """#297: core store modules never depend on M10 store modules or higher layers."""
    violations: list[str] = []
    for name in sorted(CORE_STORE_MODULES):
        path = SRC / "store" / name
        for mod in sorted(_imported_modules(path)):
            parts = mod.split(".")
            if len(parts) < 2:
                continue
            target = parts[1]
            if target == "store":
                if len(parts) >= 3 and parts[2] not in CORE_STORE_NAMES:
                    violations.append(f"{path.relative_to(REPO)} imports non-core store {mod}")
            elif target not in LEAVES:
                violations.append(f"{path.relative_to(REPO)} imports {mod}")
    assert not violations, "Core store must only import leaves/core store:\n" + "\n".join(
        violations
    )


def test_documented_module_level_layer_restrictions():
    """#297: enforce narrow module-level edges documented in docs/ARCHITECTURE.md."""
    violations: list[str] = []
    # skills/ may only import tools.registry from tools.
    for path in sorted((SRC / "skills").rglob("*.py")):
        for mod in sorted(_imported_modules(path)):
            parts = mod.split(".")
            if len(parts) >= 2 and parts[1] == "tools" and mod != "relaymind.tools.registry":
                violations.append(
                    f"{path.relative_to(REPO)} imports {mod} (only tools.registry allowed)"
                )
    # recommendations/ may only import api.errors from api.
    for path in sorted((SRC / "recommendations").rglob("*.py")):
        for mod in sorted(_imported_modules(path)):
            parts = mod.split(".")
            if len(parts) >= 2 and parts[1] == "api" and mod != "relaymind.api.errors":
                violations.append(
                    f"{path.relative_to(REPO)} imports {mod} (only api.errors allowed)"
                )
    # identifiers.py may only reference tools under TYPE_CHECKING, never at runtime.
    id_path = SRC / "identifiers.py"
    for mod in sorted(_imported_modules(id_path, include_type_checking=False)):
        parts = mod.split(".")
        if len(parts) >= 2 and parts[1] == "tools":
            violations.append(f"src/relaymind/identifiers.py imports {mod} at runtime")
    assert not violations, "Module-level layering violations:\n" + "\n".join(violations)


def test_adapters_never_import_sqlite_or_other_relaymind_packages():
    """#297: adapters stay isolated from SQLite and other RelayMind packages."""
    violations: list[str] = []
    for path in sorted((SRC / "adapters").rglob("*.py")):
        for mod in sorted(_imported_modules(path)):
            parts = mod.split(".")
            if len(parts) >= 2 and parts[1] != "adapters":
                violations.append(f"{path.relative_to(REPO)} imports {mod}")
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name == "sqlite3" or name.startswith("sqlite3."):
                    violations.append(f"{path.relative_to(REPO)} imports {name}")
    assert not violations, "Adapter isolation violations:\n" + "\n".join(violations)


def test_selection_never_creates_an_orchestrator_module_cycle():
    """#297: orchestrator modules imported by selection never import selection back."""
    imported_by_selection: set[str] = set()
    bare_orchestrator_imports: list[str] = []
    for path in sorted((SRC / "selection").rglob("*.py")):
        mods = _imported_modules(path)
        if "relaymind.orchestrator" in mods:
            bare_orchestrator_imports.append(str(path.relative_to(REPO)))
        for mod in mods:
            parts = mod.split(".")
            if len(parts) >= 3 and parts[1] == "orchestrator":
                imported_by_selection.add(parts[2])
    assert not bare_orchestrator_imports, (
        "selection must import specific orchestrator leaf modules, not the whole package: "
        + ", ".join(bare_orchestrator_imports)
    )
    back_edges: list[str] = []
    for mod_name in sorted(imported_by_selection):
        orch_file = SRC / "orchestrator" / f"{mod_name}.py"
        if orch_file.is_file():
            for mod in sorted(_imported_modules(orch_file)):
                parts = mod.split(".")
                if len(parts) >= 2 and parts[1] == "selection":
                    back_edges.append(f"{orch_file.relative_to(REPO)} imports {mod}")
    assert not back_edges, "Cycle between selection and orchestrator:\n" + "\n".join(back_edges)


def test_architecture_doc_lists_every_public_api_module():
    """#297: docs/ARCHITECTURE.md API map row lists every public src/relaymind/api module."""
    api_rows = [
        line
        for line in ARCH_DOC.read_text("utf-8").splitlines()
        if line.startswith("| API |") and "`src/relaymind/api/`" in line
    ]
    assert len(api_rows) == 1, f"Expected one API component-map row, found {len(api_rows)}"
    api_row = api_rows[0]
    api_modules = sorted(p.name for p in (SRC / "api").glob("*.py") if not p.name.startswith("_"))
    missing = [name for name in api_modules if f"`{name}`" not in api_row]
    assert not missing, f"API modules missing from docs/ARCHITECTURE.md API row: {missing}"


def test_architecture_doc_component_map_files_exist():
    """#297: every .py file named in single-directory rows of docs/ARCHITECTURE.md exists."""
    missing: list[str] = []
    checked = 0
    for line in ARCH_DOC.read_text("utf-8").splitlines():
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        dirs = re.findall(r"`(src/relaymind/(?:[^`]+/)?)`", cells[1])
        if len(dirs) != 1:
            continue
        base_dir = REPO / dirs[0]
        for filename in re.findall(r"`([A-Za-z0-9_]+\.py)`", cells[2]):
            checked += 1
            if not (base_dir / filename).is_file():
                missing.append(f"{dirs[0]}{filename}")
    assert checked >= 20
    assert not missing, f"Missing files referenced in docs/ARCHITECTURE.md component map: {missing}"


def test_skills_never_imports_upward_from_orchestrator():
    """#297: skills layer must not import orchestrator."""
    by_file: dict[str, list[str]] = defaultdict(list)
    for path in sorted((SRC / "skills").rglob("*.py")):
        for mod in sorted(_imported_modules(path)):
            parts = mod.split(".")
            if len(parts) >= 2 and parts[1] == "orchestrator":
                by_file[str(path.relative_to(REPO))].append(mod)
    assert not by_file, f"skills imports orchestrator: {dict(by_file)}"


def test_import_scanner_resolves_relative_and_package_level_imports():
    """#297: relative and package-level imports resolve to full relaymind.* paths."""
    skill_path = SRC / "skills" / "registry.py"
    found = _imported_modules(
        skill_path,
        "from ..orchestrator import router\nfrom .. import store\nfrom . import loader\n",
    )
    assert "relaymind.orchestrator.router" in found
    assert "relaymind.store" in found
    assert "relaymind.skills.loader" in found

    sel_path = SRC / "selection" / "admission.py"
    sel_found = _imported_modules(
        sel_path,
        "from relaymind.orchestrator import router\nfrom relaymind import orchestrator\n",
    )
    assert "relaymind.orchestrator.router" in sel_found
    assert "relaymind.orchestrator" in sel_found

    id_path = SRC / "identifiers.py"
    id_source = (
        "from typing import TYPE_CHECKING\n"
        "from relaymind import tools\n"
        "if TYPE_CHECKING:\n"
        "    from relaymind.tools.registry import ToolResult\n"
    )
    runtime_only = _imported_modules(id_path, id_source, include_type_checking=False)
    assert "relaymind.tools" in runtime_only
    assert "relaymind.tools.registry" not in runtime_only
