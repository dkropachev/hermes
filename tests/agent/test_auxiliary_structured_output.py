"""Structured-output (``response_format``) handling on auxiliary routes that reject it.

Covers NousResearch/hermes-agent#83390 / #105191 / #113064 for the aux callers that attach
``response_format`` (``agent/title_generator.py`` and ``agent/plugin_llm.py``): a fallback candidate
that rejects ``json_schema`` gets the same retry-without-the-field rung as the primary path instead of
aborting the task, and a route+model known to reject a ``response_format`` type (provider profile or a
capability rejection already seen in this process) never pays the guaranteed-fail first request.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import auxiliary_structured_output as structured_output
from agent.auxiliary_client import (
    _CodexCompletionsAdapter,
    _build_call_kwargs,
    _call_fallback_candidate_async,
    _call_fallback_candidate_sync,
    _is_structured_output_rejection,
    _without_structured_output_format,
)

_JSON_SCHEMA = {"type": "json_schema", "json_schema": {"name": "t", "strict": True, "schema": {"type": "object"}}}
_DEEPSEEK_400 = ("Error code: 400 - {'error': {'message': 'This response_format type is unavailable now', "
                 "'type': 'invalid_request_error', 'param': None, 'code': 'invalid_request_error'}}")


class _Rejects400(Exception):
    status_code = 400


def test_codex_adapter_maps_chat_schema_to_responses_text_format():
    adapter = _CodexCompletionsAdapter(
        SimpleNamespace(base_url="https://chatgpt.com/backend-api/codex"), "gpt-5.6-sol")
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "source_observations",
            "strict": False,
            "schema": {"type": "object", "properties": {"items": {"type": "array"}}},
        },
    }

    kwargs, _, _ = adapter._build_responses_kwargs({
        "messages": [{"role": "user", "content": "observe"}],
        "text": {"verbosity": "low", "format": {"type": "text"}},
        "extra_body": {
            "text": {"verbosity": "high"},
            "response_format": schema,
        },
    })

    assert kwargs["text"] == {
        "verbosity": "high",
        "format": {
            "type": "json_schema",
            "name": "source_observations",
            "schema": schema["json_schema"]["schema"],
            "strict": True,
        },
    }

    json_object_kwargs, _, _ = adapter._build_responses_kwargs({
        "messages": [{"role": "user", "content": "observe"}],
        "response_format": {"type": "json_object"},
    })
    assert json_object_kwargs["text"]["format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "call_kwargs, expected",
    [
        (
            {"model": "m", "text": {"format": {"type": "json_object"}, "verbosity": "high"}},
            {"model": "m", "text": {"verbosity": "high"}},
        ),
        (
            {"model": "m", "extra_body": {"text": {"format": {"type": "json_object"}, "verbosity": "low"}}},
            {"model": "m", "extra_body": {"text": {"verbosity": "low"}}},
        ),
        (
            {
                "model": "m",
                "response_format": {"type": "json_object"},
                "text": {"format": {"type": "json_object"}, "verbosity": "high"},
                "extra_body": {
                    "response_format": dict(_JSON_SCHEMA),
                    "text": {"format": {"type": "json_schema", "name": "native", "schema": {}},
                             "verbosity": "low"},
                    "metadata": {"trace": "keep"},
                },
            },
            {
                "model": "m",
                "text": {"verbosity": "high"},
                "extra_body": {
                    "text": {"verbosity": "low"},
                    "metadata": {"trace": "keep"},
                },
            },
        ),
    ],
)
def test_strip_structured_output_removes_native_and_chat_forms_but_keeps_text_options(call_kwargs, expected):
    assert _without_structured_output_format(call_kwargs) == expected


def _ok_response():
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"title": "x"}'))])


def _rejecting_client(base_url, *, async_mode=False):
    """Fake OpenAI client: 400 while ``extra_body.response_format`` is present, 200 once it is gone."""
    client = MagicMock(base_url=base_url)
    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        if "response_format" in (kwargs.get("extra_body") or {}):
            raise _Rejects400(_DEEPSEEK_400)
        return _ok_response()

    async def acreate(**kwargs):
        return create(**kwargs)

    client.chat.completions.create.side_effect = acreate if async_mode else create
    return client, sent


@pytest.mark.parametrize("async_mode", [False, True])
def test_fallback_candidate_retries_without_response_format_on_rejection(async_mode):
    """The fallback path used to re-raise the 400; now it degrades to prompt compliance like the primary path."""
    client, sent = _rejecting_client("http://relay.example:8080/v1", async_mode=async_mode)
    call = _call_fallback_candidate_async if async_mode else _call_fallback_candidate_sync
    common = dict(
        task="title_generation", messages=[{"role": "user", "content": "Reply with JSON only"}],
        temperature=None, max_tokens=64, tools=None, effective_timeout=30.0,
        effective_extra_body={"response_format": dict(_JSON_SCHEMA)}, reasoning_config=None,
    )
    result = call(client, "relay-model", "fallback_chain[0](custom)", **common)
    if async_mode:
        result = asyncio.run(result)

    assert result.choices[0].message.content == '{"title": "x"}'
    assert [("response_format" in (k.get("extra_body") or {})) for k in sent] == [True, False]
    # The rejection is remembered: the next request to this route skips the field up front.
    retry_kwargs = _build_call_kwargs(
        "custom", "relay-model", [{"role": "user", "content": "hi"}],
        extra_body={"response_format": dict(_JSON_SCHEMA)}, base_url="http://relay.example:8080/v1")
    assert "response_format" not in retry_kwargs.get("extra_body", {})


def test_known_unsupported_route_skips_response_format_before_first_request():
    """DeepSeek's profile declares json_schema unsupported; json_object and other providers are untouched."""
    messages = [{"role": "user", "content": "hi"}]
    deepseek = _build_call_kwargs(
        "deepseek", "deepseek-flash", messages, extra_body={"response_format": dict(_JSON_SCHEMA)},
        base_url="https://api.deepseek.com/v1", task="title_generation")
    assert "response_format" not in deepseek.get("extra_body", {})

    json_object = _build_call_kwargs(
        "deepseek", "deepseek-flash", messages, extra_body={"response_format": {"type": "json_object"}},
        base_url="https://api.deepseek.com/v1")
    assert json_object["extra_body"]["response_format"] == {"type": "json_object"}

    openai_kwargs = _build_call_kwargs(
        "openai", "gpt-5-mini", messages, extra_body={"response_format": dict(_JSON_SCHEMA)},
        base_url="https://api.openai.com/v1")
    assert openai_kwargs["extra_body"]["response_format"] == _JSON_SCHEMA

    # A remembered rejection is scoped to the endpoint (host:port), not to every local server.
    structured_output.remember_structured_output_rejection(
        "custom", "http://127.0.0.1:1234/v1",
        {"model": "m", "extra_body": {"response_format": dict(_JSON_SCHEMA)}}, _Rejects400(_DEEPSEEK_400))
    same = _build_call_kwargs("custom", "m", messages, extra_body={"response_format": dict(_JSON_SCHEMA)},
                              base_url="http://127.0.0.1:1234/v1")
    other = _build_call_kwargs("custom", "m", messages, extra_body={"response_format": dict(_JSON_SCHEMA)},
                               base_url="http://127.0.0.1:11434/v1")
    assert "response_format" not in same.get("extra_body", {})
    assert other["extra_body"]["response_format"] == _JSON_SCHEMA

    # A custom route pointed at DeepSeek's own host gets DeepSeek's profile.
    custom_deepseek = _build_call_kwargs(
        "custom", "deepseek-chat", messages, extra_body={"response_format": dict(_JSON_SCHEMA)},
        base_url="https://api.deepseek.com/v1")
    assert "response_format" not in custom_deepseek.get("extra_body", {})


def test_rejection_memo_is_per_model_and_ignores_schema_validation_errors():
    """One model's ``json_schema`` rejection on an aggregator host must not strip the field for every other
    model on that host, and an invalid individual schema must surface without an unconstrained retry."""
    messages = [{"role": "user", "content": "hi"}]
    openrouter = "https://openrouter.ai/api/v1"
    structured_output.remember_structured_output_rejection(
        "openrouter", openrouter,
        {"model": "some/chat-only-model", "extra_body": {"response_format": dict(_JSON_SCHEMA)}},
        _Rejects400("Error code: 400 - {'error': {'message': 'response_format is not supported by this model'}}"))
    rejected = _build_call_kwargs("openrouter", "some/chat-only-model", messages,
                                  extra_body={"response_format": dict(_JSON_SCHEMA)}, base_url=openrouter)
    sibling = _build_call_kwargs("openrouter", "openai/gpt-5-mini", messages,
                                 extra_body={"response_format": dict(_JSON_SCHEMA)}, base_url=openrouter)
    assert "response_format" not in rejected.get("extra_body", {})
    assert sibling["extra_body"]["response_format"] == _JSON_SCHEMA

    validation_400 = _Rejects400(
        "Error code: 400 - Invalid schema for response_format 'json_schema': In context=(), "
        "'additionalProperties' is required to be supplied and to be false.")
    assert not _is_structured_output_rejection(validation_400)
    assert not _is_structured_output_rejection(_Rejects400(
        "Invalid schema for text.format: json_schema requires additionalProperties=false"))
    structured_output.remember_structured_output_rejection(
        "openai", "https://api.openai.com/v1",
        {"model": "gpt-5-mini", "extra_body": {"response_format": dict(_JSON_SCHEMA)}}, validation_400)
    after = _build_call_kwargs("openai", "gpt-5-mini", messages,
                               extra_body={"response_format": dict(_JSON_SCHEMA)}, base_url="https://api.openai.com/v1")
    assert after["extra_body"]["response_format"] == _JSON_SCHEMA

    client = MagicMock(base_url="https://api.openai.com/v1")
    client.chat.completions.create.side_effect = validation_400
    with pytest.raises(_Rejects400) as raised:
        _call_fallback_candidate_sync(
            client, "gpt-5-mini", "fallback_chain[0](openai)",
            task="pr_review", messages=messages, temperature=None, max_tokens=None,
            tools=None, effective_timeout=30.0,
            effective_extra_body={"response_format": dict(_JSON_SCHEMA)}, reasoning_config=None,
        )
    assert raised.value is validation_400
    assert client.chat.completions.create.call_count == 1
    assert client.chat.completions.create.call_args.kwargs["extra_body"]["response_format"] == _JSON_SCHEMA
