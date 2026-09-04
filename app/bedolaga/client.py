"""The only code in this bot that talks to the Bedolaga Web API."""

import base64
import binascii
import logging
import time
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.bedolaga.types import (
    OPEN_STATUSES,
    TELEGRAM_ID_UNKNOWN,
    ImageAttachment,
    TelegramIdLookup,
    Ticket,
    TicketMedia,
    ticket_from_payload,
)
from app.logging_config import TRACE, log_failure
from app.logging_http import sanitize_headers, sanitize_url
from app.logging_redaction import safe_serialize
from app.retry import post_with_retry

logger = logging.getLogger(__name__)

#: What `TicketReplyRequest.message_text` accepts; longer bodies are rejected.
MAX_REPLY_LENGTH: int = 4000

#: How many tickets one status query brings back per sweep.
DEFAULT_LIST_LIMIT: int = 50

#: What the vision APIs assume when the panel does not say.
DEFAULT_MEDIA_MIME_TYPE: str = "image/jpeg"

#: The maximum media file size we allow downloading (10 MB).
MAX_MEDIA_BYTES: int = 10 * 1024 * 1024


def _fallback_media_extension(media_type: str) -> str:
    """Choose an appropriate file extension for standard media types."""
    match media_type:
        case "photo":
            return ".jpg"
        case "video" | "video_note":
            return ".mp4"
        case "animation":
            return ".gif"
        case "voice":
            return ".ogg"
        case "audio":
            return ".mp3"
        case _:
            return ".bin"


def _safe_media_filename(
    raw_name: Any,
    media_type: str,
    ticket_id: int,
    message_id: int,
) -> str:
    """Sanitize user-provided filename or construct a safe fallback."""
    if isinstance(raw_name, str):
        normalized = raw_name.replace("\\", "/")
        cleaned = PurePath(normalized).name.strip()
        cleaned = "".join(c for c in cleaned if c.isprintable() and ord(c) >= 32).strip()
        if cleaned:
            return cleaned
    ext = _fallback_media_extension(media_type)
    return f"ticket-{ticket_id}-message-{message_id}{ext}"


@dataclass(frozen=True)
class PostedTicketReply:
    """A reply the panel accepted, with its message id when the contract supplied it."""

    message_id: int | None


@dataclass(frozen=True)
class UploadedMedia:
    """Media stored by Bedolaga's Telegram bot and ready for a ticket reply."""

    media_type: str
    file_id: str


class BedolagaClient:
    """Reads tickets from Bedolaga and answers them under a service API key."""

    def __init__(self, base_url: str, api_key: str, http_client: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.http_client = http_client
        # A panel user id never changes its Telegram id, and a busy ticket asks
        # for the same one on every turn.
        self._telegram_ids: dict[int, int] = {}

    @property
    def headers(self) -> dict[str, str]:
        """The service-token header every Web API endpoint requires."""
        return {"X-API-Key": self.api_key}

    async def get_ticket(self, ticket_id: int) -> Ticket | None:
        """Read one ticket with its messages, or None when it cannot be read.

        Events carry a truncated preview at best, so every answer starts here.
        """
        url = f"{self.base_url}/tickets/{ticket_id}"
        headers = self.headers
        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE,
                "Bedolaga API get_ticket request: url=%s, headers=%s",
                sanitize_url(url),
                safe_serialize(sanitize_headers(headers)),
            )
        start_time = time.monotonic()
        try:
            response = await self.http_client.get(
                url,
                headers=headers,
            )
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API get_ticket response: status=%d, duration=%.3fs, body=%s",
                    response.status_code,
                    duration,
                    response.text,
                )
        except httpx.HTTPError as e:
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API get_ticket failed: url=%s, duration=%.3fs, error=%s",
                    sanitize_url(url),
                    duration,
                    e,
                )
            log_failure(logger, "Bedolaga ticket read failed", e, details={"ticket_id": ticket_id})
            return None

        if response.status_code != 200:
            log_failure(
                logger,
                "Bedolaga ticket read failed",
                status_code=response.status_code,
                details={"ticket_id": ticket_id},
            )
            return None

        try:
            return ticket_from_payload(response.json())
        except (ValueError, KeyError, TypeError) as e:
            log_failure(
                logger, "Bedolaga ticket response malformed", e, details={"ticket_id": ticket_id}
            )
            return None

    async def list_awaiting_ticket_ids(self, limit: int = DEFAULT_LIST_LIMIT) -> list[int]:
        """Ids of tickets in a status that means somebody is still waiting.

        The list endpoint serialises tickets without their messages, so it can
        only ever answer "which ones" — the caller reads each one in full.
        """
        ids: list[int] = []
        for status in sorted(OPEN_STATUSES):
            url = f"{self.base_url}/tickets"
            params: dict[str, Any] = {"status": status, "limit": limit}
            headers = self.headers
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API list_awaiting_ticket_ids request: url=%s, params=%s, headers=%s",
                    sanitize_url(url),
                    params,
                    safe_serialize(sanitize_headers(headers)),
                )
            start_time = time.monotonic()
            try:
                response = await self.http_client.get(
                    url,
                    headers=headers,
                    params=params,
                )
                duration = time.monotonic() - start_time
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "Bedolaga API list_awaiting_ticket_ids response (status=%s): status_code=%d, duration=%.3fs, body=%s",
                        status,
                        response.status_code,
                        duration,
                        response.text,
                    )
            except httpx.HTTPError as e:
                duration = time.monotonic() - start_time
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "Bedolaga API list_awaiting_ticket_ids failed: status=%s, duration=%.3fs, error=%s",
                        status,
                        duration,
                        e,
                    )
                log_failure(logger, "Bedolaga ticket list failed", e, ticket_status=status)
                continue

            if response.status_code != 200:
                log_failure(
                    logger,
                    "Bedolaga ticket list failed",
                    ticket_status=status,
                    status_code=response.status_code,
                )
                continue

            try:
                raw = response.json()
            except Exception as e:
                log_failure(
                    logger, "Bedolaga ticket list returned invalid JSON", e, ticket_status=status
                )
                continue

            items: list[Any] = []
            if isinstance(raw, list):
                items = raw
            elif isinstance(raw, dict) and isinstance(raw.get("items"), list):
                items = raw["items"]
            else:
                log_failure(
                    logger, "Bedolaga ticket list returned unexpected format", ticket_status=status
                )
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue
                ticket_id = item.get("id")
                if ticket_id is not None:
                    try:
                        ids.append(int(ticket_id))
                    except (TypeError, ValueError) as _:
                        log_failure(
                            logger,
                            "Bedolaga ticket list contains invalid ID",
                            ticket_status=status,
                            details={"ticket_id": ticket_id},
                        )
        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE,
                "Bedolaga API list_awaiting_ticket_ids collected %d ids: %s",
                len(ids),
                ids,
            )
        return ids

    async def reply(self, ticket_id: int, text: str) -> PostedTicketReply | None:
        """Post an answer. None means rejected; an accepted body may omit its id.

        The panel takes it from here: the message is stored as an admin reply,
        the ticket flips to `answered`, and the user gets a Telegram
        notification plus a live update in the cabinet.

        The id matters beyond "did it work". Every reply in a ticket is an
        admin message, the bot's own included, so without knowing which admin
        message ids are its own the bot cannot tell an operator working in the
        panel from itself — and would answer over a human holding the
        conversation. ``None`` means that the write failed or was rejected.
        A success whose body omits the id is still represented as an accepted
        reply, with ``message_id=None``: a write that landed must not be retried
        merely because its response body was incomplete.

        Sent exactly once, never through `post_with_retry`: all three of those
        happen on the panel's side before the response comes back, so a read
        timeout on a write that landed would resend a duplicate reply and a
        duplicate notification. The retry lives in the ticket status instead —
        a failure here leaves the message unmarked, the next sweep re-reads the
        ticket, and if the reply did land its last message is now an admin one,
        so `awaits_answer` is False and nothing is answered twice.
        """
        return await self._post_reply(
            ticket_id,
            {"message_text": text[:MAX_REPLY_LENGTH]},
        )

    async def reply_with_photo(
        self,
        ticket_id: int,
        text: str,
        base64_image: str,
        mime_type: str,
    ) -> PostedTicketReply | None:
        """Upload a Telegram photo through Bedolaga, then attach it to a reply."""
        media = await self.upload_photo(base64_image, mime_type)
        if media is None:
            return None

        caption = text[:MAX_REPLY_LENGTH]
        return await self._post_reply(
            ticket_id,
            {
                "message_text": caption or None,
                "media_type": media.media_type,
                "media_file_id": media.file_id,
                "media_caption": caption or None,
            },
        )

    async def upload_photo(
        self,
        base64_image: str,
        mime_type: str,
    ) -> UploadedMedia | None:
        """Store bytes under Bedolaga's bot token and return its transferable file id."""
        try:
            content = base64.b64decode(base64_image, validate=True)
        except (ValueError, binascii.Error) as _:
            log_failure(logger, "Bedolaga photo upload rejected: invalid base64")
            return None
        if not content or len(content) > MAX_MEDIA_BYTES:
            logger.info("Bedolaga photo upload rejected: invalid size (%d bytes)", len(content))
            return None

        extension = {
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }.get(mime_type.lower(), "jpg")
        url = f"{self.base_url}/upload"
        headers = self.headers
        filename = f"support-photo.{extension}"
        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE,
                "Bedolaga API upload_photo request: url=%s, headers=%s, filename=%s, mime_type=%s, byte_size=%d",
                sanitize_url(url),
                safe_serialize(sanitize_headers(headers)),
                filename,
                mime_type,
                len(content),
            )
        start_time = time.monotonic()
        try:
            response = await self.http_client.post(
                url,
                headers=headers,
                files={"file": (filename, content, mime_type)},
                data={"media_type": "photo"},
            )
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API upload_photo response: status_code=%d, duration=%.3fs, body=%s",
                    response.status_code,
                    duration,
                    response.text,
                )
        except httpx.HTTPError as e:
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API upload_photo failed: duration=%.3fs, error=%s",
                    duration,
                    e,
                )
            log_failure(logger, "Bedolaga photo upload failed", e)
            return None

        if response.status_code not in (200, 201):
            log_failure(
                logger,
                "Bedolaga photo upload failed",
                status_code=response.status_code,
                details=response.text,
            )
            return None

        try:
            payload = response.json() or {}
            file_id = str(payload["file_id"]).strip()
            if not file_id:
                raise ValueError("empty file_id")
            return UploadedMedia(
                media_type=str(payload.get("media_type") or "photo"),
                file_id=file_id,
            )
        except (ValueError, KeyError, TypeError) as e:
            log_failure(logger, "Bedolaga photo upload response has no file ID", e)
            return None

    async def _post_reply(
        self,
        ticket_id: int,
        payload: dict[str, Any],
    ) -> PostedTicketReply | None:
        """Post one non-idempotent ticket reply without transport retries."""
        url = f"{self.base_url}/tickets/{ticket_id}/reply"
        headers = self.headers
        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE,
                "Bedolaga API _post_reply request: url=%s, ticket_id=%d, headers=%s, payload=%s",
                sanitize_url(url),
                ticket_id,
                safe_serialize(sanitize_headers(headers)),
                safe_serialize(payload),
            )
        start_time = time.monotonic()
        try:
            response = await self.http_client.post(
                url,
                headers=headers,
                json=payload,
            )
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API _post_reply response: ticket_id=%d, status_code=%d, duration=%.3fs, body=%s",
                    ticket_id,
                    response.status_code,
                    duration,
                    response.text,
                )
        except httpx.HTTPError as e:
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API _post_reply failed: ticket_id=%d, duration=%.3fs, error=%s",
                    ticket_id,
                    duration,
                    e,
                )
            log_failure(logger, "Bedolaga ticket reply failed", e, details={"ticket_id": ticket_id})
            return None

        if response.status_code not in (200, 201):
            log_failure(
                logger,
                "Bedolaga ticket reply failed",
                status_code=response.status_code,
                details={"ticket_id": ticket_id, "body": response.text},
            )
            return None

        try:
            return PostedTicketReply(message_id=int((response.json() or {})["message"]["id"]))
        except (ValueError, KeyError, TypeError) as e:
            log_failure(
                logger,
                "Bedolaga reply response has no message ID",
                e,
                details={"ticket_id": ticket_id},
            )
            # The write landed, so it must not enter retry/backoff. Keeping the
            # missing id explicit also prevents callers from overwriting a real
            # bot-reply watermark with a fabricated sentinel such as zero.
            return PostedTicketReply(message_id=None)

    async def set_priority(self, ticket_id: int, priority: str) -> bool:
        """Raise or lower a ticket's priority. Best effort: never raises."""
        url = f"{self.base_url}/tickets/{ticket_id}/priority"
        headers = self.headers
        payload = {"priority": priority}
        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE,
                "Bedolaga API set_priority request: url=%s, ticket_id=%d, priority=%s, headers=%s",
                sanitize_url(url),
                ticket_id,
                priority,
                safe_serialize(sanitize_headers(headers)),
            )
        start_time = time.monotonic()
        try:
            response = await post_with_retry(
                self.http_client,
                url,
                headers=headers,
                json=payload,
                description=f"bedolaga priority for ticket {ticket_id}",
            )
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API set_priority response: ticket_id=%d, status_code=%d, duration=%.3fs, body=%s",
                    ticket_id,
                    response.status_code,
                    duration,
                    response.text,
                )
        except httpx.HTTPError as e:
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API set_priority failed: ticket_id=%d, duration=%.3fs, error=%s",
                    ticket_id,
                    duration,
                    e,
                )
            log_failure(
                logger, "Bedolaga priority update failed", e, details={"ticket_id": ticket_id}
            )
            return False
        return response.status_code == 200

    async def resolve_telegram_id(self, user_id: int) -> TelegramIdLookup:
        """Ask the panel for the Telegram id behind a panel user id.

        Cabinet accounts created by email or OAuth have no Telegram id at all,
        which is a normal answer here rather than a failure — so the answer is
        a `TelegramIdLookup` and not a bare `int | None`. A one-second 502 and
        an account that genuinely has no Telegram both used to come back as
        None, and the caller reads None as "this person only exists in the
        panel" and files them under a synthetic identity forever.
        """
        cached = self._telegram_ids.get(user_id)
        if cached is not None:
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API resolve_telegram_id: cache hit for user_id=%d -> telegram_id=%d",
                    user_id,
                    cached,
                )
            return TelegramIdLookup(known=True, telegram_id=cached)

        url = f"{self.base_url}/users/{user_id}"
        headers = self.headers
        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE,
                "Bedolaga API resolve_telegram_id request: url=%s, user_id=%d, headers=%s",
                sanitize_url(url),
                user_id,
                safe_serialize(sanitize_headers(headers)),
            )
        start_time = time.monotonic()
        try:
            response = await self.http_client.get(
                url,
                headers=headers,
            )
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API resolve_telegram_id response: user_id=%d, status_code=%d, duration=%.3fs, body=%s",
                    user_id,
                    response.status_code,
                    duration,
                    response.text,
                )
        except httpx.HTTPError as e:
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API resolve_telegram_id failed: user_id=%d, duration=%.3fs, error=%s",
                    user_id,
                    duration,
                    e,
                )
            log_failure(logger, "Bedolaga user read failed", e, details={"user_id": user_id})
            return TELEGRAM_ID_UNKNOWN

        if response.status_code != 200:
            log_failure(
                logger,
                "Bedolaga user read failed",
                status_code=response.status_code,
                details={"user_id": user_id},
            )
            return TELEGRAM_ID_UNKNOWN

        telegram_id = (response.json() or {}).get("telegram_id")
        if telegram_id is None:
            return TelegramIdLookup(known=True, telegram_id=None)

        resolved = int(telegram_id)
        self._telegram_ids[user_id] = resolved
        return TelegramIdLookup(known=True, telegram_id=resolved)

    def resolve_media_url(self, media_url: str) -> str | None:
        """Turn the panel's `media_url` into an absolute URL we may send the key to.

        This is the one URL in the bot that a remote response decides, and the
        request that follows carries `X-API-Key`. Relative (`/media/abc` or `media/abc`)
        is joined onto the configured base preserving any base path prefix;
        anything pointing somewhere else (different host, different effective port,
        or HTTPS -> HTTP downgrade) is dropped rather than handing the service token
        to an unverified origin.
        """
        base_parts = urlsplit(self.base_url)
        target_parts = urlsplit(media_url)

        if not target_parts.scheme and not target_parts.netloc:
            # Relative URL: join onto base_url preserving path prefix
            base_path = base_parts.path.rstrip("/")
            rel_path = target_parts.path.lstrip("/")
            combined_path = f"{base_path}/{rel_path}" if base_path else f"/{rel_path}"
            if target_parts.query:
                combined_path = f"{combined_path}?{target_parts.query}"
            absolute = f"{base_parts.scheme}://{base_parts.netloc}{combined_path}"
        else:
            absolute = media_url

        parsed = urlsplit(absolute)

        # Forbid downgrade from HTTPS to HTTP
        if base_parts.scheme == "https" and parsed.scheme != "https":
            log_failure(logger, "Bedolaga media rejected: HTTPS downgrade", details=absolute)
            return None

        if parsed.scheme != base_parts.scheme:
            log_failure(logger, "Bedolaga media rejected: scheme mismatch", details=absolute)
            return None

        # Check hostname (case-insensitive)
        if (parsed.hostname or "").lower() != (base_parts.hostname or "").lower():
            log_failure(
                logger,
                "Bedolaga media rejected: foreign host",
                details={"host": parsed.hostname, "base_host": base_parts.hostname},
            )
            return None

        # Check effective port
        parsed_port = parsed.port or (
            443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
        )
        base_port = base_parts.port or (
            443 if base_parts.scheme == "https" else 80 if base_parts.scheme == "http" else None
        )
        if parsed_port != base_port:
            log_failure(
                logger,
                "Bedolaga media rejected: port mismatch",
                details={"port": parsed_port, "base_port": base_port},
            )
            return None

        return absolute

    async def describe_media(
        self,
        ticket_id: int,
        message_id: int,
        fallback_media_type: str | None = None,
    ) -> TicketMedia | None:
        """Obtain and validate a safe media descriptor from the panel without downloading the body."""
        url = f"{self.base_url}/tickets/{ticket_id}/messages/{message_id}/media"
        headers = self.headers
        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE,
                "Bedolaga API describe_media request: url=%s, ticket_id=%d, message_id=%d, headers=%s",
                sanitize_url(url),
                ticket_id,
                message_id,
                safe_serialize(sanitize_headers(headers)),
            )
        start_time = time.monotonic()
        try:
            response = await self.http_client.get(
                url,
                headers=headers,
            )
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API describe_media response: ticket_id=%d, message_id=%d, status_code=%d, duration=%.3fs, body=%s",
                    ticket_id,
                    message_id,
                    response.status_code,
                    duration,
                    response.text,
                )
        except httpx.HTTPError as e:
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API describe_media failed: ticket_id=%d, message_id=%d, duration=%.3fs, error=%s",
                    ticket_id,
                    message_id,
                    duration,
                    e,
                )
            log_failure(
                logger, "Bedolaga media description failed", e, details={"message_id": message_id}
            )
            return None

        if response.status_code != 200:
            return None

        try:
            payload = response.json()
        except (ValueError, TypeError) as _:
            return None

        if not isinstance(payload, dict):
            return None

        media_url_raw = payload.get("media_url")
        if not media_url_raw or not isinstance(media_url_raw, str):
            logger.log(TRACE, "Bedolaga: message %d has media the panel cannot serve", message_id)
            return None

        resolved_url = self.resolve_media_url(media_url_raw)
        if resolved_url is None:
            return None

        raw_media_type = payload.get("media_type")
        if isinstance(raw_media_type, str) and raw_media_type.strip():
            media_type = raw_media_type.strip()
        elif fallback_media_type and fallback_media_type.strip():
            media_type = fallback_media_type.strip()
        else:
            media_type = "document"

        raw_filename = payload.get("file_name") or payload.get("filename")
        filename = _safe_media_filename(raw_filename, media_type, ticket_id, message_id)

        raw_mime = payload.get("mime_type")
        mime_type = raw_mime.strip() if isinstance(raw_mime, str) and raw_mime.strip() else None

        raw_size = payload.get("file_size")
        file_size: int | None = None
        if raw_size is not None:
            if isinstance(raw_size, bool):
                return None
            try:
                if isinstance(raw_size, str) and not raw_size.strip().isdigit():
                    return None
                parsed_size = int(raw_size)
                if parsed_size < 0:
                    return None
                file_size = parsed_size
            except (ValueError, TypeError) as _:
                return None

        return TicketMedia(
            media_type=media_type,
            media_url=resolved_url,
            filename=filename,
            mime_type=mime_type,
            file_size=file_size,
            download_headers=dict(self.headers),
        )

    async def download_image(self, media: TicketMedia) -> ImageAttachment | None:
        """Fetch a ticket screenshot descriptor, base64-encoded for vision APIs."""
        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE,
                "Bedolaga API download_image start: url=%s, filename=%s, media_type=%s, declared_size=%s",
                sanitize_url(media.media_url),
                media.filename,
                media.media_type,
                media.file_size,
            )
        start_time = time.monotonic()
        try:
            async with self.http_client.stream(
                "GET", media.media_url, headers=dict(media.download_headers)
            ) as downloaded:
                duration = time.monotonic() - start_time
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "Bedolaga API download_image stream connected: status_code=%d, duration=%.3fs, headers=%s",
                        downloaded.status_code,
                        duration,
                        safe_serialize(sanitize_headers(downloaded.headers)),
                    )
                if downloaded.status_code != 200:
                    return None

                declared_length = downloaded.headers.get("content-length")
                if declared_length is not None:
                    try:
                        if int(declared_length) > MAX_MEDIA_BYTES:
                            logger.info("Bedolaga media rejected: declared size exceeds limit")
                            return None
                    except ValueError:
                        log_failure(
                            logger,
                            "Bedolaga media returned invalid Content-Length",
                            details={"filename": media.filename, "content_length": declared_length},
                        )

                content = bytearray()
                async for chunk in downloaded.aiter_bytes():
                    if len(content) + len(chunk) > MAX_MEDIA_BYTES:
                        logger.info(
                            "Bedolaga media rejected: size exceeded %d bytes while streaming",
                            MAX_MEDIA_BYTES,
                        )
                        return None
                    content.extend(chunk)

                if not content:
                    return None

                mime_type = downloaded.headers.get("content-type") or DEFAULT_MEDIA_MIME_TYPE
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "Bedolaga API download_image completed: filename=%s, byte_size=%d, mime_type=%s, duration=%.3fs",
                        media.filename,
                        len(content),
                        mime_type,
                        time.monotonic() - start_time,
                    )
        except httpx.HTTPError as e:
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Bedolaga API download_image failed: url=%s, duration=%.3fs, error=%s",
                    sanitize_url(media.media_url),
                    duration,
                    e,
                )
            log_failure(
                logger, "Bedolaga media download failed", e, details={"filename": media.filename}
            )
            return None

        return ImageAttachment(
            base64_image=base64.b64encode(content).decode("ascii"),
            mime_type=mime_type.split(";")[0].strip(),
        )
