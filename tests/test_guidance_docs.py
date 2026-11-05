"""The guidance tutorial stays true to the code (RM45; section 7.7)."""

import json
import re
import tomllib
from pathlib import Path

import pytest

from relaymind.guidance import parse_guidance_settings
from relaymind.schema_util import StrictValidator, first_schema_error, load_packaged_schema
from relaymind.selection.asset_admission import _RESERVED

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "docs/guidance.md", ROOT / "docs/guidance.zh-CN.md"]


def blocks(path, language):
    return re.findall(rf"```{language}\n(.*?)```", path.read_text("utf-8"), re.S)


def dedented(text):
    return re.sub(r"(?m)^ {3}", "", text)


@pytest.mark.parametrize("path", DOCS)
def test_every_guidance_table_in_the_tutorial_starts(path):
    tables = [tomllib.loads(dedented(b)) for b in blocks(path, "toml")]
    found = [t["guidance"] for t in tables if "guidance" in t]
    assert len(found) == 2
    for table in found:
        parse_guidance_settings(table)


@pytest.mark.parametrize("path", DOCS)
def test_the_mcp_import_example_matches_the_schema(path):
    schema = load_packaged_schema("relaymind.assets", "asset_metadata_import.v1.json")
    documents = [json.loads(b) for b in blocks(path, "json") if '"removed_asset_ids"' in b]
    assert len(documents) == 1
    assert first_schema_error(StrictValidator(schema), documents[0]) is None
    assert documents[0]["records"][0]["record"]["kind"] == "mcp_tool"


@pytest.mark.parametrize("path", DOCS)
def test_the_guidance_result_example_matches_the_schema(path):
    from relaymind.guidance import validate_guidance_result

    examples = [json.loads(b) for b in blocks(path, "json") if '"chosen_by"' in b]
    assert len(examples) == 1
    validate_guidance_result(examples[0])


@pytest.mark.parametrize("path", DOCS)
def test_the_reserved_mechanisms_the_tutorial_names_are_really_reserved(path):
    text = path.read_text("utf-8")
    for name in ("lexical_overlap", "gateway_lexical_fallback"):
        assert name in text
        assert name in _RESERVED


def test_the_v2_examples_are_valid_apart_from_their_placeholders():
    path = DOCS[0]
    request = next(json.loads(b) for b in blocks(path, "json") if '"task_id"' in b)
    answer = next(json.loads(b) for b in blocks(path, "json") if '"usage_claims"' in b)
    digest = "a" * 64
    request["catalog"][0]["content_hash"] = digest
    answer["candidates"][0]["content_hash"] = digest
    for schema_name, value in (
        ("selection_request.v2.json", request),
        ("selection_result.v2.json", answer),
    ):
        schema = load_packaged_schema("relaymind.selection", schema_name)
        assert first_schema_error(StrictValidator(schema), value) is None


def test_the_example_toml_no_longer_says_http_selectors_stop_startup():
    text = (ROOT / "relaymind.example.toml").read_text("utf-8")
    assert "is not available yet" not in text
    assert "delivered by later issues" not in text
    assert "# [guidance]" in text


@pytest.mark.parametrize("guide", ["docs/asset-selection.md", "docs/asset-selection.zh-CN.md"])
def test_the_customer_guide_names_the_setting_that_turns_a_selector_on(guide):
    assert 'primary = "http"' in (ROOT / guide).read_text("utf-8")
