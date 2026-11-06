"""RM16: CI workflow files must parse before they can produce platform evidence."""

import re
from collections import Counter
from pathlib import Path

import pytest

from workflow_helpers import github_expression

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
REVIEW_FILE = WORKFLOWS / "deepseek-review.yml"
TOP_LEVEL_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(?:\s|$)", re.MULTILINE)


def test_workflows_have_unique_top_level_keys():
    """A repeated permissions, concurrency, or other top-level key invalidates YAML."""
    paths = sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")))
    assert paths
    for path in paths:
        counts = Counter(TOP_LEVEL_KEY.findall(path.read_text(encoding="utf-8")))
        duplicates = {key: count for key, count in counts.items() if count > 1}
        assert not duplicates, f"{path.name}: duplicate top-level keys {duplicates}"


def _interpolate(template: str, context: dict) -> str:
    def one(match):
        value = github_expression(match.group(1), context)
        if isinstance(value, bool):
            return "true" if value else "false"
        return "" if value is None else str(value)

    return re.sub(r"\$\{\{(.*?)\}\}", one, template)


@pytest.mark.parametrize(
    "action, changes, reviews, edit_group",
    [
        # Retargeting a stacked PR changes the diff under review: review again.
        ("edited", {"base": {"ref": {"from": "devnull/stack"}}}, True, False),
        # A title or body edit starts no review and never cancels a running one.
        ("edited", {"title": {"from": "old"}}, False, True),
        ("edited", {"body": {"from": "old"}}, False, True),
        ("synchronize", None, True, False),
        ("opened", None, True, False),
    ],
)
def test_review_runs_again_when_a_stacked_pr_is_retargeted(action, changes, reviews, edit_group):
    """RM16: a retargeted pull request is reviewed again against its new base.

    Retargeting sends a pull_request_target ``edited`` event without a new commit,
    so an approval of the old, narrower diff would otherwise stay on the commit.
    """
    import yaml

    workflow = yaml.safe_load(REVIEW_FILE.read_text("utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert "edited" in triggers["pull_request_target"]["types"]
    event = {"action": action, "pull_request": {"number": 7}}
    if changes is not None:
        event["changes"] = changes
    context = {
        "github": {
            "repository_owner": "LHCWN",
            "event_name": "pull_request_target",
            "event": event,
        }
    }
    assert bool(github_expression(workflow["jobs"]["review"]["if"], context)) is reviews
    group = _interpolate(workflow["concurrency"]["group"], context)
    assert group == ("deepseek-review-7-edit" if edit_group else "deepseek-review-7")
