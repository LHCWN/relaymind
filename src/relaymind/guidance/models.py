"""Small Chat Completions clients for the guidance stages (RM45, section 7.7).

The key is read from the environment at call time and is never kept or logged. A failed call raises,
and the pipeline records it as a failed stage without its text.
"""

import os
import re
from collections.abc import Mapping, Sequence

import httpx

from relaymind.adapters.chat_completions import RESERVED_BODY_FIELDS
from relaymind.adapters.local_http import is_loopback_url


def _clean(text: str) -> str:
    # A block cut off by the output limit has no closing tag, and must go too.
    return re.sub(r"<think>.*?(?:</think>|$)", "", text, flags=re.S).strip()


def chat_client(
    *,
    url: str,
    model: str | None = None,
    api_key_env: str | None = None,
    extra: Mapping | None = None,
    supported: Sequence[str] = ("max_output_tokens", "temperature"),
    transport: httpx.AsyncBaseTransport | None = None,
):
    """One async chat function: (messages, max_tokens) -> (text, usage or None).

    Like the main adapter, it sends a sampling option only when the provider supports it
    (`supported`, the operator's supported_options), and the declared options go in first, so
    the client's own fields win.
    """

    async def chat(messages, max_tokens):
        headers = {}
        if api_key_env is not None:
            key = os.environ.get(api_key_env)
            if not key:
                raise RuntimeError("the guidance model credential is not set")
            headers["Authorization"] = f"Bearer {key}"
        body = {k: v for k, v in (extra or {}).items() if k not in RESERVED_BODY_FIELDS}
        body.update(messages=messages, stream=False)
        if model is not None:
            body["model"] = model
        if "max_output_tokens" in supported:
            body["max_tokens"] = max_tokens
        if "temperature" in supported:
            body["temperature"] = 0
        # A loopback runtime never goes through an environment proxy (RM01).
        async with httpx.AsyncClient(
            transport=transport, trust_env=not is_loopback_url(url), timeout=None
        ) as client:
            response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage") or None
        if usage is not None:
            usage = dict(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
        return _clean(text), usage

    return chat


def local_keyword_client(local_url: str):
    """The small local model, with thinking off, as the keyword writer."""
    return chat_client(
        url=f"{local_url.rstrip('/')}/v1/chat/completions",
        extra=dict(chat_template_kwargs=dict(enable_thinking=False)),
    )


def external_client(config):
    """The configured external model; the operator's request_options are sent as declared."""
    return chat_client(
        url=f"{config.base_url.rstrip('/')}/chat/completions",
        model=config.model,
        api_key_env=config.api_key_env,
        extra=config.request_options,
        supported=getattr(config, "supported_options", ("max_output_tokens", "temperature")),
    )
