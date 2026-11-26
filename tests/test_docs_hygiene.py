"""Documentation hygiene checks for changelog format, links, and retired IDs (#298)."""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STANDARD_CHANGELOG_CATEGORIES = frozenset(
    {"Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"}
)
ACTIVE_DOCS = (
    REPO / "README.md",
    REPO / "CONTRIBUTING.md",
    REPO / "AGENTS.md",
    REPO / "CHANGELOG.md",
    *sorted((REPO / "docs").glob("*.md")),
    *sorted((REPO / "docs" / "design").glob("*.md")),
    REPO / "skills" / "README.md",
    REPO / "scripts" / "README.md",
    REPO / "models" / "README.md",
    REPO / "integrations" / "openclaw" / "README.md",
    REPO / "integrations" / "openclaw" / "README.zh-CN.md",
)


def test_changelog_uses_unique_standard_category_headings():
    """#298: each changelog section uses unique standard categories and honest release claims."""
    text = (REPO / "CHANGELOG.md").read_text("utf-8")
    assert "\n\n\n" not in text, "CHANGELOG.md contains 3+ consecutive blank lines"
    lines = text.splitlines()
    seen: set[str] = set()
    sections: list[str] = []
    for line in lines:
        if line.startswith("## "):
            sections.append(line[3:].strip())
            seen = set()
        elif line.startswith("### "):
            category = line[4:].strip()
            assert category in STANDARD_CHANGELOG_CATEGORIES, (
                f"Non-standard changelog category: {category!r}"
            )
            assert category not in seen, f"Duplicate CHANGELOG heading ### {category}"
            seen.add(category)
    assert sections
    if "keepachangelog.com" in text:
        dated = [s for s in sections if re.search(r"\d{4}-\d{2}-\d{2}", s)]
        assert dated, "Keep a Changelog claim requires dated release sections"


def test_active_docs_use_current_lhcwn_repository_url():
    """#298: active non-evidence docs never link the pre-rename organization URL."""
    stale: list[str] = []
    for path in ACTIVE_DOCS:
        for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if "Last-Humans-Coding-Under-White-Nights/relaymind" in line:
                stale.append(f"{path.relative_to(REPO)}:{lineno}")
    assert not stale, "Pre-rename org URL found in active docs:\n" + "\n".join(stale)


def test_no_unannotated_retired_ids_in_code_tests_or_scripts():
    """#298: no active code, test, or script cites retired RM30/AC23 without marking it retired."""
    offenders: list[str] = []
    id_re = re.compile(r"\b(?:RM30|AC23)\b")
    retired_re = re.compile(r"(?<!\bnot\s)\bretired\b", re.IGNORECASE)
    for root in (REPO / "src", REPO / "tests", REPO / "scripts"):
        for path in sorted(root.rglob("*.py")):
            if path.name == "test_docs_hygiene.py" or "__pycache__" in path.parts:
                continue
            lines = path.read_text("utf-8").splitlines()
            for idx, line in enumerate(lines):
                if not id_re.search(line):
                    continue
                window = " ".join(lines[max(0, idx - 1) : min(len(lines), idx + 2)])
                if not retired_re.search(window):
                    offenders.append(f"{path.relative_to(REPO)}:{idx + 1}: {line.strip()}")
    assert not offenders, "Retired RM30/AC23 cited without retired marker:\n" + "\n".join(offenders)


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


def _resolve_schema_paths(cell: str) -> list[Path]:
    paths: list[Path] = []
    base_dir: Path | None = None
    for entry in re.findall(r"`([^`]+\.json)`", cell):
        candidate = Path(entry)
        if "/" in entry:
            base_dir = candidate.parent
            paths.append(REPO / candidate)
        else:
            assert base_dir is not None, f"Relative schema filename without directory: {entry}"
            paths.append(REPO / base_dir / candidate)
    return paths


def test_specification_section_8_1_schema_status_matches_disk():
    """#298: section 8.1 Shipped vs Planned schema rows match files on disk."""
    spec = (REPO / "docs" / "SPECIFICATION.md").read_text("utf-8")
    start = spec.index("### 8.1 ")
    end = spec.find("\n### ", start + 1)
    section = spec[start:] if end == -1 else spec[start:end]
    shipped_checked = 0
    planned_checked = 0
    missing_shipped: list[str] = []
    unexpected_planned: list[str] = []
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3 or cells[0] == "Object":
            continue
        contract = cells[2]
        if re.match(r"^Shipped\b", contract):
            for full in _resolve_schema_paths(cells[1]):
                shipped_checked += 1
                if not full.is_file():
                    missing_shipped.append(str(full.relative_to(REPO)))
        elif re.match(r"^Planned\b", contract):
            for full in _resolve_schema_paths(cells[1]):
                planned_checked += 1
                if full.is_file():
                    unexpected_planned.append(str(full.relative_to(REPO)))
            # Any shipped consolidated file cited in the Contract cell must exist.
            for full in _resolve_schema_paths(contract):
                shipped_checked += 1
                if not full.is_file():
                    missing_shipped.append(str(full.relative_to(REPO)))
    assert shipped_checked >= 20
    assert planned_checked >= 1
    assert not missing_shipped, f"Shipped schemas missing on disk: {missing_shipped}"
    assert not unexpected_planned, (
        f"Planned schema paths already exist on disk (mark Shipped): {unexpected_planned}"
    )


def _github_heading_slugs(text: str) -> set[str]:
    counts: dict[str, int] = {}
    slugs: set[str] = set()
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
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


def _parse_markdown_link_target(raw: str) -> str:
    target = raw.strip()
    if target.startswith("<"):
        end = target.find(">")
        if end != -1:
            return target[1:end].strip()
    # Strip optional link title: path "title" or path 'title'
    return target.split(None, 1)[0] if target else ""


def test_active_docs_relative_links_and_anchors_resolve():
    """#298: relative markdown links and heading anchors in active docs resolve."""
    heading_cache: dict[Path, set[str]] = {}
    broken: list[str] = []
    link_re = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    inline_code_re = re.compile(r"`[^`]*`")
    repo_root = REPO.resolve()
    for path in ACTIVE_DOCS:
        in_fence = False
        for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if line.lstrip().startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            scrubbed = inline_code_re.sub("", line)
            for _, raw_target in link_re.findall(scrubbed):
                target = _parse_markdown_link_target(raw_target)
                if not target or target.startswith(("http://", "https://", "mailto:")):
                    continue
                file_part, _, anchor = target.partition("#")
                dest = path.resolve() if not file_part else (path.parent / file_part).resolve()
                if not dest.is_relative_to(repo_root):
                    broken.append(f"{path.relative_to(REPO)}:{lineno}: escapes repo ({target})")
                    continue
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
                            f"{dest.relative_to(repo_root)}"
                        )
    assert not broken, "Broken relative links or anchors in active docs:\n" + "\n".join(broken)


def test_scripts_readme_mentions_every_script():
    """#298: scripts/README.md indexes every Python script under scripts/."""
    readme = (REPO / "scripts" / "README.md").read_text("utf-8")
    missing = []
    for path in sorted((REPO / "scripts").rglob("*.py")):
        if path.name == "__init__.py" or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO / "scripts").as_posix()
        if rel not in readme and path.name not in readme:
            missing.append(rel)
    assert not missing, f"Scripts missing from scripts/README.md: {missing}"
