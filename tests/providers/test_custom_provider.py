"""Tests for OpenAICompatProvider handling custom/direct endpoints."""

from unittest.mock import AsyncMock
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nanobot.providers.openai_compat_provider import OpenAICompatProvider


def test_custom_provider_parse_handles_empty_choices() -> None:
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider()
    response = SimpleNamespace(choices=[])

    result = provider._parse(response)

    assert result.finish_reason == "error"
    assert "empty choices" in result.content


def test_custom_provider_parse_accepts_plain_string_response() -> None:
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider()

    result = provider._parse("hello from backend")

    assert result.finish_reason == "stop"
    assert result.content == "hello from backend"


def test_custom_provider_parse_accepts_dict_response() -> None:
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider()

    result = provider._parse({
        "choices": [{
            "message": {"content": "hello from dict"},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
        },
    })

    assert result.finish_reason == "stop"
    assert result.content == "hello from dict"
    assert result.usage["total_tokens"] == 3


def test_custom_provider_parse_chunks_accepts_plain_text_chunks() -> None:
    result = OpenAICompatProvider._parse_chunks(["hello ", "world"])

    assert result.finish_reason == "stop"
    assert result.content == "hello world"


@pytest.mark.asyncio
async def test_custom_provider_sends_session_identity_in_header_and_user() -> None:
    provider = OpenAICompatProvider(default_model="gpt-5.4")
    mocked_create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )
    )
    provider._client.chat.completions.create = mocked_create

    await provider.chat(
        messages=[{"role": "user", "content": "hello"}],
        session_key="telegram:chat:42",
    )

    kwargs = mocked_create.await_args.kwargs
    assert kwargs["extra_headers"] == {"x-nanobot-session-key": "telegram:chat:42"}
    assert kwargs["user"] == "nanobot:telegram:chat:42"


@pytest.mark.asyncio
async def test_custom_provider_omits_session_identity_when_missing() -> None:
    provider = OpenAICompatProvider(default_model="gpt-5.4")
    mocked_create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )
    )
    provider._client.chat.completions.create = mocked_create

    await provider.chat(messages=[{"role": "user", "content": "hello"}])

    kwargs = mocked_create.await_args.kwargs
    assert "extra_headers" not in kwargs
    assert "user" not in kwargs
