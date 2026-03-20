from __future__ import annotations

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.slack import SlackChannel
from nanobot.channels.slack import SlackConfig


class _FakeAsyncWebClient:
    def __init__(self) -> None:
        self.chat_post_calls: list[dict[str, object | None]] = []
        self.file_upload_calls: list[dict[str, object | None]] = []
        self.reactions_add_calls: list[dict[str, object | None]] = []
        self.reactions_remove_calls: list[dict[str, object | None]] = []

    async def chat_postMessage(
        self,
        *,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> None:
        self.chat_post_calls.append(
            {
                "channel": channel,
                "text": text,
                "thread_ts": thread_ts,
            }
        )

    async def files_upload_v2(
        self,
        *,
        channel: str,
        file: str,
        thread_ts: str | None = None,
    ) -> None:
        self.file_upload_calls.append(
            {
                "channel": channel,
                "file": file,
                "thread_ts": thread_ts,
            }
        )

    async def reactions_add(
        self,
        *,
        channel: str,
        name: str,
        timestamp: str,
    ) -> None:
        self.reactions_add_calls.append(
            {
                "channel": channel,
                "name": name,
                "timestamp": timestamp,
            }
        )

    async def reactions_remove(
        self,
        *,
        channel: str,
        name: str,
        timestamp: str,
    ) -> None:
        self.reactions_remove_calls.append(
            {
                "channel": channel,
                "name": name,
                "timestamp": timestamp,
            }
        )


class _FakeSocketClient:
    def __init__(self) -> None:
        self.responses: list[object] = []

    async def send_socket_mode_response(self, response: object) -> None:
        self.responses.append(response)


class _FakeSocketRequest:
    def __init__(self, payload: dict[str, object]) -> None:
        self.type = "events_api"
        self.envelope_id = "env-1"
        self.payload = payload


@pytest.mark.asyncio
async def test_send_uses_thread_for_channel_messages() -> None:
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeAsyncWebClient()
    channel._web_client = fake_web

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="C123",
            content="hello",
            media=["/tmp/demo.txt"],
            metadata={"slack": {"thread_ts": "1700000000.000100", "channel_type": "channel"}},
        )
    )

    assert len(fake_web.chat_post_calls) == 1
    assert fake_web.chat_post_calls[0]["text"] == "hello\n"
    assert fake_web.chat_post_calls[0]["thread_ts"] == "1700000000.000100"
    assert len(fake_web.file_upload_calls) == 1
    assert fake_web.file_upload_calls[0]["thread_ts"] == "1700000000.000100"


@pytest.mark.asyncio
async def test_send_omits_thread_for_dm_messages() -> None:
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeAsyncWebClient()
    channel._web_client = fake_web

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="D123",
            content="hello",
            media=["/tmp/demo.txt"],
            metadata={"slack": {"thread_ts": "1700000000.000100", "channel_type": "im"}},
        )
    )

    assert len(fake_web.chat_post_calls) == 1
    assert fake_web.chat_post_calls[0]["text"] == "hello\n"
    assert fake_web.chat_post_calls[0]["thread_ts"] is None
    assert len(fake_web.file_upload_calls) == 1
    assert fake_web.file_upload_calls[0]["thread_ts"] is None


@pytest.mark.asyncio
async def test_send_updates_reaction_when_final_response_sent() -> None:
    channel = SlackChannel(SlackConfig(enabled=True, react_emoji="eyes"), MessageBus())
    fake_web = _FakeAsyncWebClient()
    channel._web_client = fake_web

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="C123",
            content="done",
            metadata={
                "slack": {"event": {"ts": "1700000000.000100"}, "channel_type": "channel"},
            },
        )
    )

    assert fake_web.reactions_remove_calls == [
        {"channel": "C123", "name": "eyes", "timestamp": "1700000000.000100"}
    ]
    assert fake_web.reactions_add_calls == [
        {"channel": "C123", "name": "white_check_mark", "timestamp": "1700000000.000100"}
    ]


@pytest.mark.asyncio
async def test_thread_follow_up_message_continues_after_initial_mention(monkeypatch) -> None:
    channel = SlackChannel(SlackConfig(enabled=True, group_policy="mention"), MessageBus())
    fake_web = _FakeAsyncWebClient()
    fake_socket = _FakeSocketClient()
    handled: list[dict[str, object]] = []

    channel._web_client = fake_web
    channel._bot_user_id = "B123"

    async def _fake_handle_message(**kwargs):
        handled.append(kwargs)

    monkeypatch.setattr(channel, "_handle_message", _fake_handle_message)

    first_payload = {
        "event": {
            "type": "app_mention",
            "user": "U123",
            "channel": "C123",
            "channel_type": "channel",
            "text": "<@B123> hello",
            "ts": "1700000000.000100",
        }
    }
    await channel._on_socket_request(fake_socket, _FakeSocketRequest(first_payload))

    second_payload = {
        "event": {
            "type": "message",
            "user": "U123",
            "channel": "C123",
            "channel_type": "channel",
            "text": "follow up in thread",
            "ts": "1700000000.000200",
            "thread_ts": "1700000000.000100",
        }
    }
    await channel._on_socket_request(fake_socket, _FakeSocketRequest(second_payload))

    assert len(handled) == 2
    assert handled[0]["session_key"] == "slack:C123:1700000000.000100"
    assert handled[1]["session_key"] == "slack:C123:1700000000.000100"
