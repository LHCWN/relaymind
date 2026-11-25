"""Documentation hygiene checks for changelog format, links, and retired IDs (#298)."""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KEEP_A_CHANGELOG_CATEGORIES = frozenset(
    {"Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"}
)
ACTIVE_DOCS = (
    REPO / "README.md",
    REPO / "CONTRIBUTING.md",
    REPO / "AGENTS.md",
    *sorted((REPO / "docs").glob("*.md")),
)


def test_changelog_uses_unique_standard_keep_a_changelog_headings():
    """#298: each release section in CHANGELOG.md has at most one heading per standard category."""
    lines = (REPO / "CHANGELOG.md").read_text("utf-8").splitlines()
    seen: set[str] = set()
    version_sections = 0
    for line in lines:
        if line.startswith("## "):
            version_sections += 1
            seen = set()
        elif line.startswith("### "):
            category = line[4:].strip()
            assert category in KEEP_A_CHANGELOG_CATEGORIES, (
                f"Non-standard Keep a Changelog category: {category!r}"
            )
            assert category not in seen, f"Duplicate CHANGELOG heading ### {category}"
            seen.add(category)
    assert version_sections >= 1


def test_active_docs_use_current_lhcwn_repository_url():
    """#298: active docs never link the pre-rename organization URL."""
    stale: list[str] = []
    for path in ACTIVE_DOCS:
        for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if "Last-Humans-Coding-Under-White-Nights/relaymind" in line:
                stale.append(f"{path.relative_to(REPO)}:{lineno}")
    assert not stale, "Pre-rename org URL found in active docs:\n" + "\n".join(stale)


def test_no_unannotated_retired_rm30_references_in_code_or_tests():
    """#298: no active code or test cites retired RM30 without marking it retired."""
    offenders: list[str] = []
    for root in (REPO / "src", REPO / "tests"):
        for path in sorted(root.rglob("*.py")):
            if path.name == "test_docs_hygiene.py":
                continue
            for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
                if re.search(r"\bRM30\b", line) and "retired" not in line.lower():
                    offenders.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not offenders, "Retired RM30 cited without retired marker:\n" + "\n".join(offenders)


def test_release_doc_live_acceptance_test_names_exist():
    """#298: every live acceptance test node named in docs/RELEASE.md exists."""
    live_test = REPO / "tests" / "test_live_acceptance.py"
    tree = ast.parse(live_test.read_text("utf-8"), filename=str(live_test))
    defined = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    release_text = (REPO / "docs" / "RELEASE.md").read_text("utf-8")
    referenced = re.findall(
        r"tests/test_live_acceptance\.py::([A-Za-z_][A-Za-z0-9_]*)", release_text
    )
    assert referenced, "Expected at least one live acceptance test reference in docs/RELEASE.md"
    missing = [name for name in referenced if name not in defined]
    assert not missing, f"Missing live acceptance tests referenced in docs/RELEASE.md: {missing}"


def test_specification_section_8_1_shipped_schemas_exist():
    """#298: every schema row marked Shipped in docs/SPECIFICATION.md section 8.1 exists on disk."""
    spec = (REPO / "docs" / "SPECIFICATION.md").read_text("utf-8")
    start = spec.index("### 8.1 ")
    end = spec.find("\n### ", start + 1)
    section = spec[start:] if end == -1 else spec[start:end]
    checked = 0
    missing: list[str] = []
    for line in section.splitlines():
        if not line.startswith("|") or "| Shipped." not in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        paths = re.findall(r"`([^`]+\.json)`", cells[1])
        base_dir: Path | None = None
        for entry in paths:
            candidate = Path(entry)
            if "/" in entry:
                base_dir = candidate.parent
                full = REPO / candidate
            else:
                assert base_dir is not None
                full = REPO / base_dir / candidate
            checked += 1
            if not full.is_file():
                missing.append(str(full.relative_to(REPO)))
    assert checked >= 20
    assert not missing, f"Shipped schemas missing on disk: {missing}"


def _github_heading_slugs(text: str) -> set[str]:
    counts: dict[str, int] = {}
    slugs: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^#{1,6}\s+(.*)$", line)
        if not match:
            continue
        heading = match.group(1).strip().lower()
        heading = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading)
        heading = re.sub(r"[`*_~]", "", heading)
        heading = re.sub(r"[^\w\s-]", "", heading)
        base = heading.replace(" ", "-")
        seen = counts.get(base, 0)
        counts[base] = seen + 1
        slugs.add(base if seen == 0 else f"{base}-{seen}")
    return slugs


def test_active_docs_relative_links_and_anchors_resolve():
    """#298: relative markdown links and heading anchors in active docs resolve."""
    docs = (
        *ACTIVE_DOCS,
        REPO / "skills" / "README.md",
        REPO / "scripts" / "README.md",
        REPO / "models" / "README.md",
        REPO / "integrations" / "openclaw" / "README.md",
        REPO / "integrations" / "openclaw" / "README.zh-CN.md",
    )
    heading_cache: dict[Path, set[str]] = {}
    broken: list[str] = []
    link_re = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    for path in docs:
        text = path.read_text("utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for _, target in link_re.findall(line):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                file_part, _, anchor = target.partition("#")
                dest = path if not file_part else (path.parent / file_part).resolve()
                if not dest.exists():
                    broken.append(f"{path.relative_to(REPO)}:{lineno}: missing target {target}")
                    continue
                if anchor and dest.suffix == ".md":
                    slugs = heading_cache.get(dest)
                    if slugs is None:
                        slugs = _github_heading_slugs(dest.read_text("utf-8"))
                        heading_cache[dest] = slugs
                    if anchor not in slugs:
                        broken.append(
                            f"{path.relative_to(REPO)}:{lineno}: missing anchor #{anchor} in "
                            f"{dest.relative_to(REPO)}"
                        )
    assert not broken, "Broken relative links or anchors in active docs:\n" + "\n".join(broken)


def test_scripts_readme_mentions_every_top_level_script():
    """#298: scripts/README.md indexes every top-level Python script in scripts/."""
    readme = (REPO / "scripts" / "README.md").read_text("utf-8")
    missing = [
        path.name for path in sorted((REPO / "scripts").glob("*.py")) if path.name not in readme
    ]
    assert not missing, f"Top-level scripts missing from scripts/README.md: {missing}"
