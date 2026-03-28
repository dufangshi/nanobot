"""Slack channel implementation using Socket Mode."""

import asyncio
import httpx
import re
from pathlib import Path
from typing import Any

from loguru import logger
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.websockets import SocketModeClient
from slack_sdk.web.async_client import AsyncWebClient
from slackify_markdown import slackify_markdown

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from pydantic import Field

from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base


class SlackDMConfig(Base):
    """Slack DM policy configuration."""

    enabled: bool = True
    policy: str = "open"
    allow_from: list[str] = Field(default_factory=list)


class SlackConfig(Base):
    """Slack channel configuration."""

    enabled: bool = False
    mode: str = "socket"
    webhook_path: str = "/slack/events"
    bot_token: str = ""
    app_token: str = ""
    user_token_read_only: bool = True
    reply_in_thread: bool = True
    react_emoji: str = "eyes"
    done_emoji: str = "white_check_mark"
    allow_from: list[str] = Field(default_factory=list)
    group_policy: str = "mention"
    group_allow_from: list[str] = Field(default_factory=list)
    max_media_bytes: int = 20 * 1024 * 1024
    dm: SlackDMConfig = Field(default_factory=SlackDMConfig)


class SlackChannel(BaseChannel):
    """Slack channel using Socket Mode."""

    name = "slack"
    display_name = "Slack"
    _SUPPORTED_MESSAGE_SUBTYPES = {None, "file_share"}
    _IMAGE_PREFIX = "image/"
    _AUDIO_PREFIX = "audio/"
    _PDF_MIME = "application/pdf"
    _SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
    _MAX_DOWNLOAD_REDIRECTS = 5
    _DOWNLOAD_RETRY_DELAYS = (0.5, 1.0, 2.0)
    _DOWNLOAD_ERROR_LIMIT = 240
    _DOWNLOAD_PREVIEW_LIMIT = 160

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return SlackConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = SlackConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: SlackConfig = config
        self._web_client: AsyncWebClient | None = None
        self._socket_client: SocketModeClient | None = None
        self._bot_user_id: str | None = None
        self._active_threads: set[tuple[str, str]] = set()

    async def start(self) -> None:
        """Start the Slack Socket Mode client."""
        if not self.config.bot_token or not self.config.app_token:
            logger.error("Slack bot/app token not configured")
            return
        if self.config.mode != "socket":
            logger.error("Unsupported Slack mode: {}", self.config.mode)
            return

        self._running = True

        self._web_client = AsyncWebClient(token=self.config.bot_token)
        self._socket_client = SocketModeClient(
            app_token=self.config.app_token,
            web_client=self._web_client,
        )

        self._socket_client.socket_mode_request_listeners.append(self._on_socket_request)

        # Resolve bot user ID for mention handling
        try:
            auth = await self._web_client.auth_test()
            self._bot_user_id = auth.get("user_id")
            logger.info("Slack bot connected as {}", self._bot_user_id)
        except Exception as e:
            logger.warning("Slack auth_test failed: {}", e)

        logger.info("Starting Slack Socket Mode client...")
        await self._socket_client.connect()

        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the Slack client."""
        self._running = False
        if self._socket_client:
            try:
                await self._socket_client.close()
            except Exception as e:
                logger.warning("Slack socket close failed: {}", e)
            self._socket_client = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Slack."""
        if not self._web_client:
            logger.warning("Slack client not running")
            return
        try:
            slack_meta = msg.metadata.get("slack", {}) if msg.metadata else {}
            thread_ts = slack_meta.get("thread_ts")
            channel_type = slack_meta.get("channel_type")
            # Slack DMs don't use threads; channel/group replies may keep thread_ts.
            thread_ts_param = thread_ts if thread_ts and channel_type != "im" else None

            # Slack rejects empty text payloads. Keep media-only messages media-only,
            # but send a single blank message when the bot has no text or files to send.
            if msg.content or not (msg.media or []):
                await self._web_client.chat_postMessage(
                    channel=msg.chat_id,
                    text=self._to_mrkdwn(msg.content) if msg.content else " ",
                    thread_ts=thread_ts_param,
                )

            for media_path in msg.media or []:
                try:
                    await self._web_client.files_upload_v2(
                        channel=msg.chat_id,
                        file=media_path,
                        thread_ts=thread_ts_param,
                    )
                except Exception as e:
                    logger.error("Failed to upload file {}: {}", media_path, e)

            # Update reaction emoji when the final (non-progress) response is sent
            if not (msg.metadata or {}).get("_progress"):
                event = slack_meta.get("event", {})
                await self._update_react_emoji(msg.chat_id, event.get("ts"))

        except Exception as e:
            logger.error("Error sending Slack message: {}", e)
            raise

    async def _on_socket_request(
        self,
        client: SocketModeClient,
        req: SocketModeRequest,
    ) -> None:
        """Handle incoming Socket Mode requests."""
        if req.type != "events_api":
            return

        # Acknowledge right away
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        payload = req.payload or {}
        event = payload.get("event") or {}
        event_type = event.get("type")

        # Handle app mentions or plain messages
        if event_type not in ("message", "app_mention"):
            return

        sender_id = event.get("user")
        chat_id = event.get("channel")
        subtype = event.get("subtype")

        if subtype not in self._SUPPORTED_MESSAGE_SUBTYPES:
            return
        if event.get("bot_id"):
            return
        if self._bot_user_id and sender_id == self._bot_user_id:
            return

        # Avoid double-processing: Slack sends both `message` and `app_mention`
        # for mentions in channels. Prefer `app_mention`.
        text = event.get("text") or ""
        if (
            event_type == "message"
            and subtype is None
            and self._bot_user_id
            and f"<@{self._bot_user_id}>" in text
        ):
            return

        # Debug: log basic event shape
        logger.debug(
            "Slack event: type={} subtype={} user={} channel={} channel_type={} text={}",
            event_type,
            event.get("subtype"),
            sender_id,
            chat_id,
            event.get("channel_type"),
            text[:80],
        )
        if not sender_id or not chat_id:
            return

        channel_type = event.get("channel_type") or ""

        if not self._is_allowed(sender_id, chat_id, channel_type):
            return

        thread_ts = event.get("thread_ts")

        if channel_type != "im" and not self._should_respond_in_channel(
            event_type,
            text,
            chat_id,
            thread_ts,
        ):
            return

        text = self._strip_bot_mention(text)
        media_paths, attachment_parts, attachments_meta = await self._collect_inbound_media(event)

        if self.config.reply_in_thread and not thread_ts:
            thread_ts = event.get("ts")
        if thread_ts and channel_type != "im":
            self._active_threads.add((chat_id, thread_ts))
        # Add :eyes: reaction to the triggering message (best-effort)
        try:
            if self._web_client and event.get("ts"):
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name=self.config.react_emoji,
                    timestamp=event.get("ts"),
                )
        except Exception as e:
            logger.debug("Slack reactions_add failed: {}", e)

        # Thread-scoped session key for channel/group messages
        session_key = f"slack:{chat_id}:{thread_ts}" if thread_ts and channel_type != "im" else None

        try:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content="\n".join(part for part in [text, *attachment_parts] if part) or "[empty message]",
                media=media_paths,
                metadata={
                    "slack": {
                        "event": event,
                        "thread_ts": thread_ts,
                        "channel_type": channel_type,
                    },
                    "attachments": attachments_meta,
                },
                session_key=session_key,
            )
        except Exception:
            logger.exception("Error handling Slack message from {}", sender_id)

    @classmethod
    def _safe_filename(cls, value: str | None, default: str) -> str:
        raw = (value or "").strip()
        if not raw:
            return default
        name = Path(raw).name.strip().replace("\x00", "")
        name = cls._SAFE_NAME_RE.sub("_", name).strip("._")
        return name or default

    @classmethod
    def _attachment_mode(cls, file_obj: dict[str, Any]) -> str:
        mimetype = str(file_obj.get("mimetype") or "").lower()
        if mimetype.startswith(cls._IMAGE_PREFIX):
            return "image"
        if mimetype.startswith(cls._AUDIO_PREFIX):
            return "audio"
        return "file"

    @staticmethod
    def _is_html_response(raw: bytes, content_type: str | None) -> bool:
        ctype = (content_type or "").lower()
        if "text/html" in ctype or "application/xhtml+xml" in ctype:
            return True
        prefix = raw.lstrip()[:256].lower()
        return prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html")

    @staticmethod
    def _is_expected_pdf(file_obj: dict[str, Any], filename: str) -> bool:
        mimetype = str(file_obj.get("mimetype") or "").lower()
        if mimetype == SlackChannel._PDF_MIME:
            return True
        return filename.lower().endswith(".pdf")

    @staticmethod
    def _is_valid_pdf(raw: bytes) -> bool:
        return raw.startswith(b"%PDF-")

    @classmethod
    def _sanitize_download_error(cls, reason: str | None) -> str:
        text = re.sub(r"\s+", " ", str(reason or "")).strip()
        if not text:
            return "unknown error"
        text = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [redacted]", text, flags=re.IGNORECASE)
        text = re.sub(r"xox[a-z]-[A-Za-z0-9-]+", "[redacted-slack-token]", text, flags=re.IGNORECASE)
        if len(text) > cls._DOWNLOAD_ERROR_LIMIT:
            text = text[: cls._DOWNLOAD_ERROR_LIMIT - 3].rstrip() + "..."
        return text

    @classmethod
    def _sanitize_download_url(cls, url: str | None) -> str:
        if not url:
            return ""
        try:
            parsed = httpx.URL(url)
            return str(parsed.copy_with(query=None, fragment=None))
        except Exception:
            return str(url).split("?", 1)[0].split("#", 1)[0]

    @classmethod
    def _body_preview(cls, raw: bytes) -> str:
        text = raw[: cls._DOWNLOAD_PREVIEW_LIMIT].decode("utf-8", errors="replace")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @classmethod
    def _build_download_error(
        cls,
        base: str,
        response_meta: dict[str, Any] | None = None,
        raw: bytes | None = None,
    ) -> str:
        parts = [base]
        meta = response_meta or {}
        final_url = cls._sanitize_download_url(meta.get("final_url"))
        if final_url:
            parts.append(f"url={final_url}")
        status_code = meta.get("status_code")
        if status_code:
            parts.append(f"status={status_code}")
        content_type = meta.get("content_type")
        if content_type:
            parts.append(f"content-type={content_type}")
        content_length = meta.get("content_length")
        if content_length not in (None, ""):
            parts.append(f"content-length={content_length}")
        redirects = meta.get("redirects") or []
        if redirects:
            parts.append(f"redirects={len(redirects)}")
        if raw:
            preview = cls._body_preview(raw)
            if preview:
                parts.append(f"preview={preview}")
        return " | ".join(parts)

    @classmethod
    def _download_failure_marker(cls, filename: str, reason: str | None) -> str:
        return f"[attachment: {filename} - download failed: {cls._sanitize_download_error(reason)}]"

    @staticmethod
    def _allows_auth_redirect(url: httpx.URL) -> bool:
        host = (url.host or "").lower()
        return (
            host == "slack.com"
            or host.endswith(".slack.com")
            or host.endswith(".slack-edge.com")
        )

    async def _download_bytes_with_redirects(self, url: str) -> tuple[bytes, dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self.config.bot_token}"}
        current = url
        redirects: list[str] = []
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
            for _ in range(self._MAX_DOWNLOAD_REDIRECTS + 1):
                response = await client.get(current, headers=headers)
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("redirect missing location")
                    next_url = response.url.join(location)
                    redirects.append(
                        f"{self._sanitize_download_url(str(response.url))} -> {self._sanitize_download_url(str(next_url))}"
                    )
                    current = str(next_url)
                    headers = headers if self._allows_auth_redirect(next_url) else {}
                    continue
                content_type = response.headers.get("content-type")
                content_length = response.headers.get("content-length")
                if response.status_code >= 400:
                    raise RuntimeError(
                        self._build_download_error(
                            f"HTTP {response.status_code}",
                            {
                                "final_url": str(response.url),
                                "status_code": response.status_code,
                                "content_type": content_type,
                                "content_length": content_length,
                                "redirects": redirects,
                            },
                            response.content,
                        )
                    )
                return response.content, {
                    "final_url": str(response.url),
                    "status_code": response.status_code,
                    "content_type": content_type,
                    "content_length": content_length,
                    "redirects": redirects,
                }
        raise RuntimeError("too many redirects")

    async def _resolve_file_object(self, file_obj: dict[str, Any]) -> dict[str, Any] | None:
        resolved, _ = await self._resolve_file_object_with_error(file_obj)
        return resolved

    async def _resolve_file_object_with_error(
        self,
        file_obj: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not self._web_client:
            return file_obj, None
        if file_obj.get("file_access") != "check_file_info":
            return file_obj, None
        file_id = file_obj.get("id")
        if not file_id:
            return None, "missing Slack file id"
        try:
            resp = await self._web_client.files_info(file=file_id)
        except Exception as e:
            logger.warning("Slack files.info failed for {}: {}", file_id, e)
            return None, f"files.info failed: {e}"
        resolved = resp.get("file") if isinstance(resp, dict) else None
        if isinstance(resolved, dict):
            return resolved, None
        return None, "files.info returned no file payload"

    async def _download_slack_file(
        self,
        file_obj: dict[str, Any],
    ) -> tuple[str | None, str | None, dict[str, Any]]:
        file_id = str(file_obj.get("id") or "")
        mode = self._attachment_mode(file_obj)
        size = int(file_obj.get("size") or 0)
        filename = self._safe_filename(
            str(file_obj.get("name") or file_obj.get("title") or ""),
            f"file_{file_id or 'attachment'}",
        )
        meta = {
            "id": file_id,
            "name": file_obj.get("name") or filename,
            "title": file_obj.get("title") or "",
            "mimetype": file_obj.get("mimetype") or "",
            "size": size,
            "path": "",
            "download_url": file_obj.get("url_private") or file_obj.get("url_private_download") or "",
            "mode": mode,
            "error": "",
            "response_url": "",
            "response_content_type": "",
            "response_content_length": "",
            "redirects": [],
        }
        limit = max(int(self.config.max_media_bytes), 0)
        if limit == 0 or (size and size > limit):
            return None, f"[attachment: {filename} - too large]", meta

        urls = []
        for candidate in (file_obj.get("url_private"), file_obj.get("url_private_download")):
            if isinstance(candidate, str) and candidate and candidate not in urls:
                urls.append(candidate)
        if not urls or not self.config.bot_token:
            reason = "missing Slack file download URL" if not urls else "missing Slack bot token"
            meta["error"] = self._sanitize_download_error(reason)
            return None, self._download_failure_marker(filename, reason), meta

        media_dir = get_media_dir("slack")
        local_name = self._safe_filename(f"{file_id}_{filename}" if file_id else filename, filename)
        file_path = media_dir / local_name

        last_error: Exception | None = None
        for attempt, delay in enumerate((0.0, *self._DOWNLOAD_RETRY_DELAYS), start=1):
            for url in urls:
                try:
                    raw, response_meta = await self._download_bytes_with_redirects(url)
                    meta["response_url"] = self._sanitize_download_url(response_meta.get("final_url"))
                    meta["response_content_type"] = response_meta.get("content_type") or ""
                    meta["response_content_length"] = str(response_meta.get("content_length") or "")
                    meta["redirects"] = list(response_meta.get("redirects") or [])
                    content_type = response_meta.get("content_type")
                    if self._is_html_response(raw, content_type):
                        raise RuntimeError(
                            self._build_download_error("received HTML instead of file bytes", response_meta, raw)
                        )
                    if self._is_expected_pdf(file_obj, filename) and not self._is_valid_pdf(raw):
                        raise RuntimeError(
                            self._build_download_error("downloaded file is not a valid PDF", response_meta, raw)
                        )
                    file_path.write_bytes(raw)
                    meta["download_url"] = url
                    meta["path"] = str(file_path)
                    return str(file_path), f"[attachment: {file_path}]", meta
                except Exception as e:
                    last_error = e
                    if not meta["response_url"]:
                        meta["response_url"] = self._sanitize_download_url(url)
                    logger.warning(
                        "Slack attachment download attempt {} failed for {} via {}: {}",
                        attempt,
                        file_id or filename,
                        url,
                        e,
                    )
            if delay > 0:
                await asyncio.sleep(delay)
                if self._web_client and file_id:
                    refreshed = await self._resolve_file_object({"id": file_id, "file_access": "check_file_info"})
                    if refreshed:
                        file_obj = refreshed
                        urls = []
                        for candidate in (file_obj.get("url_private"), file_obj.get("url_private_download")):
                            if isinstance(candidate, str) and candidate and candidate not in urls:
                                urls.append(candidate)

        if last_error is not None:
            logger.warning("Failed to download Slack attachment {}: {}", file_id or filename, last_error)
        meta["error"] = self._sanitize_download_error(str(last_error) if last_error else None)
        return None, self._download_failure_marker(filename, meta["error"]), meta

    async def _collect_inbound_media(
        self,
        event: dict[str, Any],
    ) -> tuple[list[str], list[str], list[dict[str, Any]]]:
        media_paths: list[str] = []
        content_parts: list[str] = []
        attachments_meta: list[dict[str, Any]] = []

        for raw_file in event.get("files") or []:
            if not isinstance(raw_file, dict):
                continue
            file_obj, resolve_error = await self._resolve_file_object_with_error(raw_file)
            if not file_obj:
                fallback_name = self._safe_filename(str(raw_file.get("name") or ""), "attachment")
                error = self._sanitize_download_error(resolve_error or "unable to resolve Slack file metadata")
                attachments_meta.append(
                    {
                        "id": str(raw_file.get("id") or ""),
                        "name": raw_file.get("name") or fallback_name,
                        "title": raw_file.get("title") or "",
                        "mimetype": raw_file.get("mimetype") or "",
                        "size": int(raw_file.get("size") or 0),
                        "path": "",
                        "download_url": raw_file.get("url_private") or raw_file.get("url_private_download") or "",
                        "mode": self._attachment_mode(raw_file),
                        "error": error,
                        "response_url": "",
                        "response_content_type": "",
                        "response_content_length": "",
                        "redirects": [],
                    }
                )
                content_parts.append(self._download_failure_marker(fallback_name, error))
                continue

            path, marker, meta = await self._download_slack_file(file_obj)
            attachments_meta.append(meta)
            if marker:
                content_parts.append(marker)
            if not path:
                continue
            media_paths.append(path)
            if meta["mode"] == "audio":
                transcription = await self.transcribe_audio(path)
                if transcription:
                    content_parts.append(f"[transcription: {transcription}]")

        return media_paths, content_parts, attachments_meta

    async def _update_react_emoji(self, chat_id: str, ts: str | None) -> None:
        """Remove the in-progress reaction and optionally add a done reaction."""
        if not self._web_client or not ts:
            return
        try:
            await self._web_client.reactions_remove(
                channel=chat_id,
                name=self.config.react_emoji,
                timestamp=ts,
            )
        except Exception as e:
            logger.debug("Slack reactions_remove failed: {}", e)
        if self.config.done_emoji:
            try:
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name=self.config.done_emoji,
                    timestamp=ts,
                )
            except Exception as e:
                logger.debug("Slack done reaction failed: {}", e)

    def _is_allowed(self, sender_id: str, chat_id: str, channel_type: str) -> bool:
        if channel_type == "im":
            if not self.config.dm.enabled:
                return False
            if self.config.dm.policy == "allowlist":
                return sender_id in self.config.dm.allow_from
            return True

        # Group / channel messages
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return True

    def _should_respond_in_channel(
        self,
        event_type: str,
        text: str,
        chat_id: str,
        thread_ts: str | None = None,
    ) -> bool:
        if self.config.group_policy == "open":
            return True
        if thread_ts and (chat_id, thread_ts) in self._active_threads:
            return True
        if self.config.group_policy == "mention":
            if event_type == "app_mention":
                return True
            return self._bot_user_id is not None and f"<@{self._bot_user_id}>" in text
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return False

    def _strip_bot_mention(self, text: str) -> str:
        if not text or not self._bot_user_id:
            return text
        return re.sub(rf"<@{re.escape(self._bot_user_id)}>\s*", "", text).strip()

    _TABLE_RE = re.compile(r"(?m)^\|.*\|$(?:\n\|[\s:|-]*\|$)(?:\n\|.*\|$)*")
    _CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
    _INLINE_CODE_RE = re.compile(r"`[^`]+`")
    _LEFTOVER_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    _LEFTOVER_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
    _BARE_URL_RE = re.compile(r"(?<![|<])(https?://\S+)")

    @classmethod
    def _to_mrkdwn(cls, text: str) -> str:
        """Convert Markdown to Slack mrkdwn, including tables."""
        if not text:
            return ""
        text = cls._TABLE_RE.sub(cls._convert_table, text)
        return cls._fixup_mrkdwn(slackify_markdown(text))

    @classmethod
    def _fixup_mrkdwn(cls, text: str) -> str:
        """Fix markdown artifacts that slackify_markdown misses."""
        code_blocks: list[str] = []

        def _save_code(m: re.Match) -> str:
            code_blocks.append(m.group(0))
            return f"\x00CB{len(code_blocks) - 1}\x00"

        text = cls._CODE_FENCE_RE.sub(_save_code, text)
        text = cls._INLINE_CODE_RE.sub(_save_code, text)
        text = cls._LEFTOVER_BOLD_RE.sub(r"*\1*", text)
        text = cls._LEFTOVER_HEADER_RE.sub(r"*\1*", text)
        text = cls._BARE_URL_RE.sub(lambda m: m.group(0).replace("&amp;", "&"), text)

        for i, block in enumerate(code_blocks):
            text = text.replace(f"\x00CB{i}\x00", block)
        return text

    @staticmethod
    def _convert_table(match: re.Match) -> str:
        """Convert a Markdown table to a Slack-readable list."""
        lines = [ln.strip() for ln in match.group(0).strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return match.group(0)
        headers = [h.strip() for h in lines[0].strip("|").split("|")]
        start = 2 if re.fullmatch(r"[|\s:\-]+", lines[1]) else 1
        rows: list[str] = []
        for line in lines[start:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            cells = (cells + [""] * len(headers))[: len(headers)]
            parts = [f"**{headers[i]}**: {cells[i]}" for i in range(len(headers)) if cells[i]]
            if parts:
                rows.append(" · ".join(parts))
        return "\n".join(rows)
