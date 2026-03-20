from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from nanobot.providers.custom_provider import CustomProvider


def test_custom_provider_parse_handles_empty_choices() -> None:
    provider = CustomProvider()
    response = SimpleNamespace(choices=[])

    result = provider._parse(response)

    assert result.finish_reason == "error"
    assert "empty choices" in result.content


@pytest.mark.asyncio
async def test_custom_provider_sends_session_identity_in_header_and_user() -> None:
    provider = CustomProvider(default_model="gpt-5.4")
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
    provider = CustomProvider(default_model="gpt-5.4")
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
