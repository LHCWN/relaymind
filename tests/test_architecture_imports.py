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
    {"__init__.py", "run_store.py", "trace_event.py", "model_timings.py", "outcomes.py"}
)
CORE_STORE_NAMES = frozenset(
    name.removesuffix(".py") for name in CORE_STORE_MODULES if name != "__init__.py"
)

# Documented package-level allowed dependencies from docs/ARCHITECTURE.md.
ALLOWED_PACKAGE_EDGES: dict[str, frozenset[str]] = {
    "__init__": frozenset(),
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
    ("stats", "adapters"): frozenset({"relaymind.adapters.base"}),
    ("tools", "adapters"): frozenset({"relaymind.adapters.base"}),
    ("skills", "tools"): frozenset({"relaymind.tools.registry"}),
    ("identifiers", "tools"): frozenset({"relaymind.tools.registry"}),
    ("selection", "orchestrator"): frozenset(
        {
            "relaymind.orchestrator.router",
            "relaymind.orchestrator.route_decision",
            "relaymind.orchestrator.placement",
        }
    ),
    ("assets", "tools"): frozenset({"relaymind.tools.registry"}),
    ("assets", "recommendations"): frozenset(),
    ("assets", "selection"): frozenset(
        {
            "relaymind.selection.asset_contract",
            "relaymind.selection.asset_lexical",
            "relaymind.selection.http",
        }
    ),
    ("recommendations", "api"): frozenset(),
    ("guidance", "assets"): frozenset({"relaymind.assets.catalog"}),
    ("guidance", "recommendations"): frozenset({"relaymind.recommendations.limits"}),
    ("guidance", "selection"): frozenset(
        {
            "relaymind.selection.asset_admission",
            "relaymind.selection.asset_contract",
            "relaymind.selection.asset_http",
            "relaymind.selection.asset_lexical",
            "relaymind.selection.http",
        }
    ),
}

# File-scoped exceptions documented in docs/ARCHITECTURE.md.
ALLOWED_FILE_MODULE_EDGES: dict[tuple[str, str], frozenset[str]] = {
    ("assets/embedding_bundle.py", "selection"): frozenset({"relaymind.selection"}),
    ("assets/index_lifecycle.py", "recommendations"): frozenset(
        {"relaymind.recommendations.service"}
    ),
    ("recommendations/errors.py", "api"): frozenset({"relaymind.api.errors"}),
}

V2_SELECTION_MODULES = frozenset({"adaptation_stage.py"})
V2_SELECTION_ALLOWED_V1_HELPERS = frozenset({"chain", "http"})
M10_ONLY_TARGETS = frozenset({"assets", "recommendations", "store"})
V2_SELECTION_ALLOWED_TARGETS = M10_ONLY_TARGETS | LEAVES
CORE_STORE_ONLY_CALLERS = frozenset({"tools", "orchestrator", "ui"})
V2_SELECTION_ONLY_CALLERS = frozenset({"store", "recommendations"})


def _is_v2_selection_name(mod_name: str) -> bool:
    return mod_name.startswith("asset_") or f"{mod_name}.py" in V2_SELECTION_MODULES


def _submodule_in_package(pkg: str, mod: str) -> str | None:
    """Return the direct submodule name if mod refers to a file/subpackage inside pkg."""
    parts = mod.split(".")
    if len(parts) < 3 or parts[1] != pkg:
        return None
    name = parts[2]
    if (SRC / pkg / f"{name}.py").is_file() or (SRC / pkg / name).is_dir():
        return name
    return None


def test_package_imports_match_documented_architecture_contract():
    """#297: every cross-package import obeys docs/ARCHITECTURE.md."""
    violations: list[str] = []
    discovered_packages: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        pkg = _package_of(path)
        rel_src = path.relative_to(SRC).as_posix()
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
            if allowed_mods is not None:
                extra_mods = ALLOWED_FILE_MODULE_EDGES.get((rel_src, target), frozenset())
                if mod not in (allowed_mods | extra_mods):
                    violations.append(
                        f"{path.relative_to(REPO)}: {pkg} -> {mod} "
                        f"(allowed: {sorted(allowed_mods | extra_mods)})"
                    )
                    continue
            # Core store callers (tools, orchestrator, ui) may only import core store modules
            # (or symbols re-exported from store/__init__.py, which is itself core-store-only).
            if pkg in CORE_STORE_ONLY_CALLERS and target == "store":
                submod = _submodule_in_package("store", mod)
                if submod is not None and submod not in CORE_STORE_NAMES:
                    violations.append(
                        f"{path.relative_to(REPO)}: {pkg} -> non-core store module {mod}"
                    )
            # Orchestrator may only import v1 selection modules (never v2 asset_*/adaptation_stage).
            if pkg == "orchestrator" and target == "selection":
                submod = _submodule_in_package("selection", mod)
                if submod is not None and _is_v2_selection_name(submod):
                    violations.append(
                        f"{path.relative_to(REPO)}: orchestrator -> v2 selection module {mod}"
                    )
            # M10 store and recommendations modules may only import v2 selection modules.
            if pkg in V2_SELECTION_ONLY_CALLERS and target == "selection":
                submod = _submodule_in_package("selection", mod)
                if submod is None or not _is_v2_selection_name(submod):
                    violations.append(
                        f"{path.relative_to(REPO)}: {pkg} -> non-v2 selection module {mod}"
                    )
    assert not violations, "Forbidden cross-layer imports:\n" + "\n".join(violations)
    assert discovered_packages <= set(ALLOWED_PACKAGE_EDGES)


def test_core_store_modules_depend_only_on_leaves():
    """#297: core store modules (including store/__init__.py) never depend on M10 or higher."""
    violations: list[str] = []
    for name in sorted(CORE_STORE_MODULES):
        path = SRC / "store" / name
        for mod in sorted(_imported_modules(path)):
            parts = mod.split(".")
            if len(parts) < 2:
                continue
            target = parts[1]
            if target == "store":
                if len(parts) < 3 or parts[2] not in CORE_STORE_NAMES:
                    violations.append(f"{path.relative_to(REPO)} imports non-core store {mod}")
            elif target not in LEAVES:
                violations.append(f"{path.relative_to(REPO)} imports {mod}")
    assert not violations, "Core store must only import leaves/core store:\n" + "\n".join(
        violations
    )


def test_type_checking_only_and_v1_selection_restrictions():
    """#297: enforce TYPE_CHECKING-only edges and strict v1/v2 selection isolation."""
    violations: list[str] = []
    # identifiers.py may only reference tools under TYPE_CHECKING, never at runtime.
    id_path = SRC / "identifiers.py"
    for mod in sorted(_imported_modules(id_path, include_type_checking=False)):
        parts = mod.split(".")
        if len(parts) >= 2 and parts[1] == "tools":
            violations.append(f"src/relaymind/identifiers.py imports {mod} at runtime")
    # assets/ may only reference recommendations under TYPE_CHECKING, never at runtime.
    for path in sorted((SRC / "assets").rglob("*.py")):
        for mod in sorted(_imported_modules(path, include_type_checking=False)):
            parts = mod.split(".")
            if len(parts) >= 2 and parts[1] == "recommendations":
                violations.append(f"{path.relative_to(REPO)} imports {mod} at runtime")
    # Strict v1/v2 selection isolation:
    # - v1 selection (including selection/__init__.py) must not import M10 packages or v2 modules.
    # - v2 selection (asset_*.py, adaptation_stage.py) must only import M10/leaf packages
    #   (beyond intra-package selection helpers).
    for path in sorted((SRC / "selection").rglob("*.py")):
        is_v2 = path.name.startswith("asset_") or path.name in V2_SELECTION_MODULES
        for mod in sorted(_imported_modules(path)):
            parts = mod.split(".")
            if len(parts) < 2:
                continue
            target = parts[1]
            if not is_v2:
                if target in M10_ONLY_TARGETS:
                    violations.append(f"{path.relative_to(REPO)} (v1 selection) imports {mod}")
                elif target == "selection":
                    submod = _submodule_in_package("selection", mod)
                    if submod is not None and _is_v2_selection_name(submod):
                        violations.append(
                            f"{path.relative_to(REPO)} (v1 selection) imports v2 module {mod}"
                        )
            else:
                if target == "selection":
                    submod = _submodule_in_package("selection", mod)
                    if submod is None or (
                        not _is_v2_selection_name(submod)
                        and submod not in V2_SELECTION_ALLOWED_V1_HELPERS
                    ):
                        violations.append(
                            f"{path.relative_to(REPO)} (v2 selection) imports v1 module {mod}"
                        )
                elif target not in V2_SELECTION_ALLOWED_TARGETS:
                    violations.append(f"{path.relative_to(REPO)} (v2 selection) imports {mod}")
    assert not violations, "Type-checking / v1-v2 selection violations:\n" + "\n".join(violations)


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


def _resolve_orchestrator_module(subparts: tuple[str, ...]) -> Path:
    """Resolve full dotted parts under orchestrator/ (including nested subpackages)."""
    for end in range(len(subparts), 0, -1):
        prefix = subparts[:end]
        file_path = SRC / "orchestrator" / Path(*prefix[:-1], f"{prefix[-1]}.py")
        if file_path.is_file():
            return file_path
        pkg_init = SRC / "orchestrator" / Path(*prefix) / "__init__.py"
        if pkg_init.is_file():
            return pkg_init
    return SRC / "orchestrator" / "__init__.py"


def test_selection_never_creates_an_orchestrator_module_cycle():
    """#297: orchestrator modules imported by selection never reach selection transitively."""
    imported_by_selection: set[tuple[str, ...]] = set()
    bare_orchestrator_imports: list[str] = []
    for path in sorted((SRC / "selection").rglob("*.py")):
        mods = _imported_modules(path)
        if "relaymind.orchestrator" in mods:
            bare_orchestrator_imports.append(str(path.relative_to(REPO)))
        for mod in mods:
            parts = mod.split(".")
            if len(parts) >= 3 and parts[1] == "orchestrator":
                imported_by_selection.add(tuple(parts[2:]))
    assert not bare_orchestrator_imports, (
        "selection must import specific orchestrator leaf modules, not the whole package: "
        + ", ".join(bare_orchestrator_imports)
    )
    # Transitive DFS across orchestrator modules reachable from selection/.
    back_edges: list[str] = []
    visited: set[Path] = set()
    stack: list[Path] = [
        _resolve_orchestrator_module(subparts) for subparts in sorted(imported_by_selection)
    ]
    while stack:
        orch_file = stack.pop()
        if orch_file in visited or not orch_file.is_file():
            continue
        visited.add(orch_file)
        for mod in sorted(_imported_modules(orch_file)):
            parts = mod.split(".")
            if len(parts) < 2:
                continue
            if parts[1] == "selection":
                back_edges.append(f"{orch_file.relative_to(REPO)} imports {mod}")
            elif parts[1] == "orchestrator":
                if len(parts) == 2:
                    stack.append(SRC / "orchestrator" / "__init__.py")
                else:
                    stack.append(_resolve_orchestrator_module(tuple(parts[2:])))
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
    api_files_cell = api_row.strip("|").split("|")[2]
    api_modules = sorted(p.name for p in (SRC / "api").glob("*.py") if not p.name.startswith("_"))
    missing = [name for name in api_modules if f"`{name}`" not in api_files_cell]
    assert not missing, f"API modules missing from docs/ARCHITECTURE.md API row: {missing}"


def test_architecture_doc_component_map_files_exist():
    """#297: every file named in docs/ARCHITECTURE.md component map rows exists on disk."""
    missing: list[str] = []
    checked = 0
    for line in ARCH_DOC.read_text("utf-8").splitlines():
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        # Verify explicit file paths in the Directory column (e.g. api/guidance.py).
        for explicit_file in re.findall(r"`(src/relaymind/[^`]+\.py)`", cells[1]):
            checked += 1
            if not (REPO / explicit_file).is_file():
                missing.append(explicit_file)
        dirs = re.findall(r"`(src/relaymind/(?:[^`]+/)?)`", cells[1])
        dirs = [d for d in dirs if d.endswith("/")]
        if not dirs:
            continue
        for filename in re.findall(r"`([A-Za-z0-9_]+\.py)`", cells[2]):
            checked += 1
            if not any((REPO / d / filename).is_file() for d in dirs):
                missing.append(f"{dirs}: {filename}")
    assert checked >= 25
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
