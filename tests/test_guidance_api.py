"""The guidance endpoint, its model clients and launch settings (RM45, AC38; section 7.7)."""

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from launcher_helpers import write_config
from relaymind.api.guidance import install_guidance_routes
from relaymind.guidance import GuidanceSettings, parse_guidance_settings
from relaymind.guidance.models import chat_client
from relaymind.launcher import LaunchError, build_app, load_config

BODY = dict(
    schema_version=1,
    task_text="merge these pdf files",
    assets=[dict(name="pdf-merge", kind="skill", description="Merge pdf documents")],
)
AUTH = {"Authorization": "Bearer token"}


def client(settings=None, **kw):
    app = FastAPI()
    app.state.application_token = "token"
    install_guidance_routes(app, settings or GuidanceSettings(enabled=True), **kw)
    return TestClient(app)


def test_guidance_answers_with_the_exact_offered_asset():
    answer = client().post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert answer.status_code == 200
    assert answer.json()["picks"] == [dict(name="pdf-merge", kind="skill")]


def test_the_application_token_is_required():
    assert client().post("/relay/v1/guidance", json=BODY).status_code == 401


def test_disabled_guidance_is_unavailable():
    answer = client(GuidanceSettings()).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert answer.status_code == 503
    assert answer.json()["error"]["code"] == "recommendation_unavailable"


@pytest.mark.parametrize("raw", [b"not json", b"{}", b'{"schema_version":1,"task_text":"x"}'])
def test_a_bad_body_is_invalid_request(raw):
    answer = client().post("/relay/v1/guidance", content=raw, headers=AUTH)
    assert answer.status_code == 400
    assert answer.json()["error"]["code"] == "invalid_request"


def test_a_body_over_the_limit_is_refused(monkeypatch):
    monkeypatch.setattr("relaymind.api.guidance.MAX_GUIDANCE_BYTES", 10)
    answer = client().post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert answer.status_code == 400


def test_a_failing_model_still_answers_200_and_says_so():
    async def down(messages, max_tokens):
        raise RuntimeError("secret-detail")

    settings = GuidanceSettings(enabled=True, picker="external")
    answer = client(settings, picker=down).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert answer.status_code == 200
    assert answer.json()["degraded"] is True
    assert "secret-detail" not in answer.text


def test_nothing_is_kept_in_the_store_or_app_state():
    app = FastAPI()
    app.state.application_token = "token"
    install_guidance_routes(app, GuidanceSettings(enabled=True))
    before = dict(app.state._state)
    TestClient(app).post("/relay/v1/guidance", json=BODY, headers=AUTH)
    assert dict(app.state._state) == before


# --- model clients --------------------------------------------------------------------------


def recording(answer, seen):
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=answer)

    return httpx.MockTransport(handler)


def reply(text, **usage):
    return dict(choices=[dict(message=dict(content=text))], usage=usage)


async def _call(chat):
    return await chat([dict(role="user", content="x")], 50)


def test_the_local_client_turns_thinking_off_and_strips_it(monkeypatch):
    import asyncio

    seen = []
    chat = chat_client(
        url="http://127.0.0.1:8033/v1/chat/completions",
        transport=recording(
            reply("<think>hm</think> merge pdf", prompt_tokens=7, completion_tokens=2), seen
        ),
        extra=dict(chat_template_kwargs=dict(enable_thinking=False)),
    )
    text, usage = asyncio.run(_call(chat))
    assert text == "merge pdf"
    assert usage == dict(input_tokens=7, output_tokens=2)
    sent = json.loads(seen[0].content)
    assert sent["chat_template_kwargs"] == dict(enable_thinking=False)
    assert "authorization" not in seen[0].headers


def test_the_external_client_reads_the_key_at_call_time_and_sends_options(monkeypatch):
    import asyncio

    seen = []
    monkeypatch.setenv("TEST_KEY", "abc")
    chat = chat_client(
        url="https://example.invalid/v1/chat/completions",
        model="m",
        api_key_env="TEST_KEY",
        extra=dict(reasoning_effort="low"),
        transport=recording(reply("ok"), seen),
    )
    assert asyncio.run(_call(chat)) == ("ok", None)
    assert seen[0].headers["authorization"] == "Bearer abc"
    assert json.loads(seen[0].content)["model"] == "m"
    assert json.loads(seen[0].content)["reasoning_effort"] == "low"


def test_a_client_with_a_missing_key_fails_without_a_call(monkeypatch):
    import asyncio

    seen = []
    monkeypatch.delenv("TEST_KEY", raising=False)
    chat = chat_client(
        url="https://example.invalid/v1/chat/completions",
        api_key_env="TEST_KEY",
        transport=recording(reply("ok"), seen),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(_call(chat))
    assert seen == []


# --- launch settings ------------------------------------------------------------------------


def test_guidance_is_off_unless_the_table_says_so(tmp_path):
    assert load_config(write_config(tmp_path)).guidance.enabled is False


def test_the_launch_file_can_turn_guidance_on(tmp_path):
    config = load_config(
        write_config(tmp_path, "\n[guidance]\nenabled = true\nkeywords = 'local'\n")
    )
    assert config.guidance == parse_guidance_settings(dict(enabled=True, keywords="local"))


@pytest.mark.parametrize(
    "table",
    [
        "\n[guidance]\nnope = 1\n",
        "\n[guidance]\npicker = 'external'\n",
        "\n[guidance]\nkeywords = 'external'\n",
    ],
)
def test_bad_guidance_settings_stop_startup(tmp_path, table):
    with pytest.raises(LaunchError):
        load_config(write_config(tmp_path, table))


def test_local_keywords_need_a_local_model(tmp_path):
    external = (
        "\n[external_model]\nbase_url = 'https://example.invalid/v1'\n"
        "model = 'm'\ncontext_limit = 4096\n"
    )
    table = "\n[guidance]\nenabled = true\nkeywords = 'local'\n"
    with pytest.raises(LaunchError):
        load_config(write_config(tmp_path, external + table, local=False))


def test_the_launched_gateway_serves_guidance_with_no_model_call(tmp_path):
    from helpers import ScriptedAdapter

    config = load_config(write_config(tmp_path, "\n[guidance]\nenabled = true\n"))
    launched = build_app(config, local_adapter=ScriptedAdapter(turns=[]), external_adapter=None)
    token = launched.app.state.application_token
    with TestClient(launched.app) as http:
        answer = http.post(
            "/relay/v1/guidance", json=BODY, headers={"Authorization": f"Bearer {token}"}
        )
    assert answer.status_code == 200
    assert answer.json()["chosen_by"] == "word_match"


def test_a_launched_gateway_without_the_table_refuses_guidance(tmp_path):
    from helpers import ScriptedAdapter

    config = load_config(write_config(tmp_path))
    launched = build_app(config, local_adapter=ScriptedAdapter(turns=[]), external_adapter=None)
    token = launched.app.state.application_token
    with TestClient(launched.app) as http:
        answer = http.post(
            "/relay/v1/guidance", json=BODY, headers={"Authorization": f"Bearer {token}"}
        )
    assert answer.status_code == 503


def test_an_unclosed_thinking_block_is_removed_from_a_model_answer():
    """Review of PR 285: an answer cut off inside <think> must not leak the thinking."""
    from relaymind.guidance.models import _clean

    assert _clean("<think>long thoughts that never end") == ""
    assert _clean('<think>a</think>{"picks":[1]}') == '{"picks":[1]}'
    assert _clean('{"picks":[1]} <think>late') == '{"picks":[1]}'


def test_the_client_sends_only_the_sampling_options_the_provider_supports():
    """A provider that refuses temperature, declared in supported_options, gets none."""
    import asyncio

    seen = []
    chat = chat_client(
        url="https://example.invalid/v1/chat/completions",
        model="m",
        supported=("max_output_tokens",),
        extra=dict(model="other", reasoning_effort="low"),
        transport=recording(reply("ok"), seen),
    )
    asyncio.run(_call(chat))
    sent = json.loads(seen[0].content)
    assert "temperature" not in sent and sent["max_tokens"] == 50
    assert sent["model"] == "m"  # The gateway's own fields win over declared options.
    assert sent["reasoning_effort"] == "low"


def test_the_external_client_follows_the_configured_supported_options():
    from types import SimpleNamespace

    from relaymind.guidance.models import external_client

    config = SimpleNamespace(
        base_url="https://example.invalid/v1",
        model="m",
        api_key_env=None,
        request_options=None,
        supported_options=("max_output_tokens",),
    )
    assert external_client(config) is not None
