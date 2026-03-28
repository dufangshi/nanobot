from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.providers.transcription import OpenAITranscriptionProvider


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, calls: list[dict[str, object]]) -> None:
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url: str, *, headers: dict[str, str], files: dict[str, object], timeout: float):
        self._calls.append(
            {
                "url": url,
                "headers": headers,
                "files": files,
                "timeout": timeout,
            }
        )
        return _FakeResponse({"text": "transcribed by openai"})


@pytest.mark.asyncio
async def test_openai_transcription_provider_posts_audio(monkeypatch, tmp_path: Path) -> None:
    import nanobot.providers.transcription as transcription_module

    audio_path = tmp_path / "clip.m4a"
    audio_path.write_bytes(b"audio")
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        transcription_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(calls),
    )

    provider = OpenAITranscriptionProvider(
        api_key="sk-openai-test",
        api_base="https://api.openai.com/v1",
        model="gpt-4o-mini-transcribe",
    )
    text = await provider.transcribe(audio_path)

    assert text == "transcribed by openai"
    assert calls[0]["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert calls[0]["headers"] == {"Authorization": "Bearer sk-openai-test"}
    assert calls[0]["files"]["model"] == (None, "gpt-4o-mini-transcribe")
    assert calls[0]["files"]["file"][0] == "clip.m4a"
