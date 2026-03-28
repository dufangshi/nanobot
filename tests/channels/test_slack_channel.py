from __future__ import annotations

from pathlib import Path

import pytest

# Check optional Slack dependencies before running tests
try:
    import slack_sdk  # noqa: F401
except ImportError:
    pytest.skip("Slack dependencies not installed (slack-sdk)", allow_module_level=True)

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
        self.files_info_calls: list[dict[str, object | None]] = []
        self.files_info_response: dict[str, object] | None = None

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

    async def files_info(self, *, file: str) -> dict[str, object]:
        self.files_info_calls.append({"file": file})
        return self.files_info_response or {"ok": False}


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


class _FakeHTTPResponse:
    def __init__(
        self,
        *,
        url: str,
        status_code: int,
        headers: dict[str, str] | None = None,
        content: bytes = b"",
    ) -> None:
        self.url = __import__("httpx").URL(url)
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHTTPClient:
    def __init__(self, responses: list[_FakeHTTPResponse], calls: list[dict[str, object]]) -> None:
        self._responses = list(responses)
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url: str, headers: dict[str, str] | None = None):
        self._calls.append({"url": url, "headers": dict(headers or {})})
        if not self._responses:
            raise RuntimeError("No fake HTTP responses left")
        return self._responses.pop(0)


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


@pytest.mark.asyncio
async def test_file_share_message_downloads_attachment_and_passes_media(monkeypatch, tmp_path) -> None:
    channel = SlackChannel(SlackConfig(enabled=True, group_policy="open", allow_from=["*"]), MessageBus())
    fake_web = _FakeAsyncWebClient()
    fake_socket = _FakeSocketClient()
    handled: list[dict[str, object]] = []
    attachment_path = tmp_path / "demo.txt"
    attachment_path.write_text("hello", encoding="utf-8")

    channel._web_client = fake_web
    channel._bot_user_id = "B123"

    async def _fake_handle_message(**kwargs):
        handled.append(kwargs)

    async def _fake_download(file_obj):
        return str(attachment_path), f"[attachment: {attachment_path}]", {
            "id": file_obj.get("id"),
            "name": file_obj.get("name"),
            "title": "",
            "mimetype": file_obj.get("mimetype") or "text/plain",
            "size": file_obj.get("size") or 5,
            "path": str(attachment_path),
            "download_url": "https://example.com/demo.txt",
            "mode": "file",
        }

    monkeypatch.setattr(channel, "_handle_message", _fake_handle_message)
    monkeypatch.setattr(channel, "_download_slack_file", _fake_download)

    payload = {
        "event": {
            "type": "message",
            "subtype": "file_share",
            "user": "U123",
            "channel": "C123",
            "channel_type": "channel",
            "text": "here is a file",
            "ts": "1700000000.000100",
            "files": [{"id": "F123", "name": "demo.txt", "mimetype": "text/plain", "size": 5}],
        }
    }
    await channel._on_socket_request(fake_socket, _FakeSocketRequest(payload))

    assert len(handled) == 1
    assert handled[0]["media"] == [str(attachment_path)]
    assert "[attachment: " in handled[0]["content"]
    assert handled[0]["metadata"]["attachments"][0]["path"] == str(attachment_path)


@pytest.mark.asyncio
async def test_check_file_info_fetches_full_file_before_download(monkeypatch, tmp_path) -> None:
    channel = SlackChannel(SlackConfig(enabled=True, group_policy="open", allow_from=["*"]), MessageBus())
    fake_web = _FakeAsyncWebClient()
    fake_socket = _FakeSocketClient()
    handled: list[dict[str, object]] = []
    attachment_path = tmp_path / "resolved.txt"
    attachment_path.write_text("hello", encoding="utf-8")

    fake_web.files_info_response = {
        "ok": True,
        "file": {"id": "F123", "name": "resolved.txt", "mimetype": "text/plain", "size": 5},
    }
    channel._web_client = fake_web
    channel._bot_user_id = "B123"

    async def _fake_handle_message(**kwargs):
        handled.append(kwargs)

    async def _fake_download(file_obj):
        assert file_obj["name"] == "resolved.txt"
        return str(attachment_path), f"[attachment: {attachment_path}]", {
            "id": file_obj.get("id"),
            "name": file_obj.get("name"),
            "title": "",
            "mimetype": file_obj.get("mimetype") or "text/plain",
            "size": file_obj.get("size") or 5,
            "path": str(attachment_path),
            "download_url": "https://example.com/resolved.txt",
            "mode": "file",
        }

    monkeypatch.setattr(channel, "_handle_message", _fake_handle_message)
    monkeypatch.setattr(channel, "_download_slack_file", _fake_download)

    payload = {
        "event": {
            "type": "message",
            "subtype": "file_share",
            "user": "U123",
            "channel": "C123",
            "channel_type": "channel",
            "text": "",
            "ts": "1700000000.000100",
            "files": [{"id": "F123", "file_access": "check_file_info"}],
        }
    }
    await channel._on_socket_request(fake_socket, _FakeSocketRequest(payload))

    assert fake_web.files_info_calls == [{"file": "F123"}]
    assert len(handled) == 1
    assert handled[0]["media"] == [str(attachment_path)]


@pytest.mark.asyncio
async def test_audio_attachment_adds_transcription(monkeypatch, tmp_path) -> None:
    channel = SlackChannel(SlackConfig(enabled=True, group_policy="open", allow_from=["*"]), MessageBus())
    fake_web = _FakeAsyncWebClient()
    fake_socket = _FakeSocketClient()
    handled: list[dict[str, object]] = []
    attachment_path = tmp_path / "clip.mp3"
    attachment_path.write_bytes(b"audio")

    channel._web_client = fake_web
    channel._bot_user_id = "B123"

    async def _fake_handle_message(**kwargs):
        handled.append(kwargs)

    async def _fake_download(file_obj):
        return str(attachment_path), f"[attachment: {attachment_path}]", {
            "id": file_obj.get("id"),
            "name": file_obj.get("name"),
            "title": "",
            "mimetype": "audio/mpeg",
            "size": 5,
            "path": str(attachment_path),
            "download_url": "https://example.com/clip.mp3",
            "mode": "audio",
        }

    async def _fake_transcribe(_path):
        return "hello world"

    monkeypatch.setattr(channel, "_handle_message", _fake_handle_message)
    monkeypatch.setattr(channel, "_download_slack_file", _fake_download)
    monkeypatch.setattr(channel, "transcribe_audio", _fake_transcribe)

    payload = {
        "event": {
            "type": "message",
            "subtype": "file_share",
            "user": "U123",
            "channel": "C123",
            "channel_type": "channel",
            "text": "voice note",
            "ts": "1700000000.000100",
            "files": [{"id": "F123", "name": "clip.mp3", "mimetype": "audio/mpeg", "size": 5}],
        }
    }
    await channel._on_socket_request(fake_socket, _FakeSocketRequest(payload))

    assert len(handled) == 1
    assert "[attachment: " in handled[0]["content"]
    assert "[transcription: hello world]" in handled[0]["content"]


@pytest.mark.asyncio
async def test_file_download_failure_still_forwards_message(monkeypatch) -> None:
    channel = SlackChannel(SlackConfig(enabled=True, group_policy="open", allow_from=["*"]), MessageBus())
    fake_web = _FakeAsyncWebClient()
    fake_socket = _FakeSocketClient()
    handled: list[dict[str, object]] = []

    channel._web_client = fake_web
    channel._bot_user_id = "B123"

    async def _fake_handle_message(**kwargs):
        handled.append(kwargs)

    async def _fake_download(file_obj):
        return None, "[attachment: broken.txt - download failed]", {
            "id": file_obj.get("id"),
            "name": "broken.txt",
            "title": "",
            "mimetype": "text/plain",
            "size": 5,
            "path": "",
            "download_url": "https://example.com/broken.txt",
            "mode": "file",
        }

    monkeypatch.setattr(channel, "_handle_message", _fake_handle_message)
    monkeypatch.setattr(channel, "_download_slack_file", _fake_download)

    payload = {
        "event": {
            "type": "message",
            "subtype": "file_share",
            "user": "U123",
            "channel": "C123",
            "channel_type": "channel",
            "text": "please inspect",
            "ts": "1700000000.000100",
            "files": [{"id": "F123", "name": "broken.txt", "mimetype": "text/plain", "size": 5}],
        }
    }
    await channel._on_socket_request(fake_socket, _FakeSocketRequest(payload))

    assert len(handled) == 1
    assert handled[0]["media"] == []
    assert "[attachment: broken.txt - download failed]" in handled[0]["content"]


@pytest.mark.asyncio
async def test_download_slack_file_marks_large_attachment(monkeypatch) -> None:
    channel = SlackChannel(
        SlackConfig(enabled=True, bot_token="xoxb-test", max_media_bytes=4),
        MessageBus(),
    )

    path, marker, meta = await channel._download_slack_file(
        {"id": "F123", "name": "big.bin", "mimetype": "application/octet-stream", "size": 5}
    )

    assert path is None
    assert marker == "[attachment: big.bin - too large]"
    assert meta["path"] == ""


@pytest.mark.asyncio
async def test_download_slack_file_preserves_auth_across_slack_redirect(monkeypatch, tmp_path) -> None:
    import nanobot.channels.slack as slack_module

    channel = SlackChannel(
        SlackConfig(enabled=True, bot_token="xoxb-test", max_media_bytes=10_000),
        MessageBus(),
    )
    calls: list[dict[str, object]] = []
    responses = [
        _FakeHTTPResponse(
            url="https://files.slack.com/files-pri/T1-F123/report.pdf",
            status_code=302,
            headers={"location": "https://downloads.slack.com/files-pri/T1-F123/report.pdf"},
        ),
        _FakeHTTPResponse(
            url="https://downloads.slack.com/files-pri/T1-F123/report.pdf",
            status_code=200,
            headers={"content-type": "application/pdf"},
            content=b"%PDF-1.7\nfake pdf",
        ),
    ]

    monkeypatch.setattr(
        slack_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeHTTPClient(responses, calls),
    )

    path, marker, meta = await channel._download_slack_file(
        {
            "id": "F123",
            "name": "report.pdf",
            "mimetype": "application/pdf",
            "size": 16,
            "url_private_download": "https://files.slack.com/files-pri/T1-F123/report.pdf",
        }
    )

    assert path is not None
    assert marker is not None and "attachment" in marker
    assert Path(path).read_bytes().startswith(b"%PDF-")
    assert calls[0]["headers"] == {"Authorization": "Bearer xoxb-test"}
    assert calls[1]["headers"] == {"Authorization": "Bearer xoxb-test"}
    assert meta["path"] == path


@pytest.mark.asyncio
async def test_download_slack_file_rejects_html_payload(monkeypatch) -> None:
    import nanobot.channels.slack as slack_module

    channel = SlackChannel(
        SlackConfig(enabled=True, bot_token="xoxb-test", max_media_bytes=10_000),
        MessageBus(),
    )
    responses = [
        _FakeHTTPResponse(
            url="https://files.slack.com/files-pri/T1-F123/report.pdf",
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<!DOCTYPE html><html lang='en-US'><head></head><body>login</body></html>",
        ),
    ]
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        slack_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeHTTPClient(responses, calls),
    )

    path, marker, meta = await channel._download_slack_file(
        {
            "id": "F123",
            "name": "report.pdf",
            "mimetype": "application/pdf",
            "size": 72,
            "url_private_download": "https://files.slack.com/files-pri/T1-F123/report.pdf",
        }
    )

    assert path is None
    assert marker == "[attachment: report.pdf - download failed]"
    assert meta["path"] == ""


@pytest.mark.asyncio
async def test_download_slack_file_rejects_non_pdf_payload_for_pdf(monkeypatch) -> None:
    import nanobot.channels.slack as slack_module

    channel = SlackChannel(
        SlackConfig(enabled=True, bot_token="xoxb-test", max_media_bytes=10_000),
        MessageBus(),
    )
    responses = [
        _FakeHTTPResponse(
            url="https://files.slack.com/files-pri/T1-F123/report.pdf",
            status_code=200,
            headers={"content-type": "application/octet-stream"},
            content=b"not actually a pdf",
        ),
    ]
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        slack_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeHTTPClient(responses, calls),
    )

    path, marker, meta = await channel._download_slack_file(
        {
            "id": "F123",
            "name": "report.pdf",
            "mimetype": "application/pdf",
            "size": 18,
            "url_private_download": "https://files.slack.com/files-pri/T1-F123/report.pdf",
        }
    )

    assert path is None
    assert marker == "[attachment: report.pdf - download failed]"
    assert meta["path"] == ""
