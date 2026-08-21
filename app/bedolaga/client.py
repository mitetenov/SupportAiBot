"""The only code in this bot that talks to the Bedolaga Web API."""

import base64
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.bedolaga.types import (
    OPEN_STATUSES,
    TELEGRAM_ID_UNKNOWN,
    ImageAttachment,
    TelegramIdLookup,
    Ticket,
    ticket_from_payload,
)
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


@dataclass(frozen=True)
class PostedTicketReply:
    """A reply the panel accepted, with its message id when the contract supplied it."""

    message_id: int | None


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
        try:
            response = await self.http_client.get(
                f"{self.base_url}/tickets/{ticket_id}",
                headers=self.headers,
            )
        except httpx.HTTPError as e:
            logger.warning("Bedolaga: could not read ticket %d: %s", ticket_id, e)
            return None

        if response.status_code != 200:
            logger.warning(
                "Bedolaga: reading ticket %d returned %d", ticket_id, response.status_code
            )
            return None

        try:
            return ticket_from_payload(response.json())
        except (ValueError, KeyError, TypeError) as e:
            logger.warning("Bedolaga: ticket %d came back malformed: %s", ticket_id, e)
            return None

    async def list_awaiting_ticket_ids(self, limit: int = DEFAULT_LIST_LIMIT) -> list[int]:
        """Ids of tickets in a status that means somebody is still waiting.

        The list endpoint serialises tickets without their messages, so it can
        only ever answer "which ones" — the caller reads each one in full.
        """
        ids: list[int] = []
        for status in sorted(OPEN_STATUSES):
            try:
                response = await self.http_client.get(
                    f"{self.base_url}/tickets",
                    headers=self.headers,
                    params={"status": status, "limit": limit},
                )
            except httpx.HTTPError as e:
                logger.warning("Bedolaga: could not list %s tickets: %s", status, e)
                continue

            if response.status_code != 200:
                logger.warning(
                    "Bedolaga: listing %s tickets returned %d", status, response.status_code
                )
                continue

            try:
                raw = response.json()
            except Exception as e:
                logger.warning("Bedolaga: listing %s tickets returned invalid JSON: %s", status, e)
                continue

            items: list[Any] = []
            if isinstance(raw, list):
                items = raw
            elif isinstance(raw, dict) and isinstance(raw.get("items"), list):
                items = raw["items"]
            else:
                logger.warning(
                    "Bedolaga: listing %s tickets returned unexpected payload format", status
                )
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue
                ticket_id = item.get("id")
                if ticket_id is not None:
                    try:
                        ids.append(int(ticket_id))
                    except TypeError, ValueError:
                        logger.warning(
                            "Bedolaga: unparseable ticket id %r in %s list", ticket_id, status
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
        try:
            response = await self.http_client.post(
                f"{self.base_url}/tickets/{ticket_id}/reply",
                headers=self.headers,
                json={"message_text": text[:MAX_REPLY_LENGTH]},
            )
        except httpx.HTTPError as e:
            logger.error("Bedolaga: replying to ticket %d failed: %s", ticket_id, e)
            return None

        if response.status_code not in (200, 201):
            logger.error(
                "Bedolaga: replying to ticket %d returned %d: %s",
                ticket_id,
                response.status_code,
                response.text[:200],
            )
            return None

        try:
            return PostedTicketReply(message_id=int((response.json() or {})["message"]["id"]))
        except (ValueError, KeyError, TypeError) as e:
            logger.warning(
                "Bedolaga: the reply to ticket %d landed but came back without a message id: %s",
                ticket_id,
                e,
            )
            # The write landed, so it must not enter retry/backoff. Keeping the
            # missing id explicit also prevents callers from overwriting a real
            # bot-reply watermark with a fabricated sentinel such as zero.
            return PostedTicketReply(message_id=None)

    async def set_priority(self, ticket_id: int, priority: str) -> bool:
        """Raise or lower a ticket's priority. Best effort: never raises."""
        try:
            response = await post_with_retry(
                self.http_client,
                f"{self.base_url}/tickets/{ticket_id}/priority",
                headers=self.headers,
                json={"priority": priority},
                description=f"bedolaga priority for ticket {ticket_id}",
            )
        except httpx.HTTPError as e:
            logger.warning("Bedolaga: setting priority on ticket %d failed: %s", ticket_id, e)
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
            return TelegramIdLookup(known=True, telegram_id=cached)

        try:
            response = await self.http_client.get(
                f"{self.base_url}/users/{user_id}",
                headers=self.headers,
            )
        except httpx.HTTPError as e:
            logger.warning("Bedolaga: could not read user %d: %s", user_id, e)
            return TELEGRAM_ID_UNKNOWN

        if response.status_code != 200:
            logger.warning("Bedolaga: reading user %d returned %d", user_id, response.status_code)
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
            logger.warning("Bedolaga: refusing HTTPS -> HTTP downgrade for media: %s", absolute)
            return None

        if parsed.scheme != base_parts.scheme:
            logger.warning("Bedolaga: refusing scheme mismatch for media: %s", absolute)
            return None

        # Check hostname (case-insensitive)
        if (parsed.hostname or "").lower() != (base_parts.hostname or "").lower():
            logger.warning(
                "Bedolaga: refusing to fetch media from a foreign host: %s (base %s)",
                parsed.hostname,
                base_parts.hostname,
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
            logger.warning(
                "Bedolaga: refusing media with different port %s (base %s)",
                parsed_port,
                base_port,
            )
            return None

        return absolute

    async def download_media(self, ticket_id: int, message_id: int) -> ImageAttachment | None:
        """Fetch a ticket screenshot, base64-encoded for the vision APIs.

        The `media_file_id` on the message is a Telegram file id belonging to
        the Bedolaga bot, which this bot's token cannot resolve — the bytes
        have to come back through their API, under the same service key.
        """
        try:
            described = await self.http_client.get(
                f"{self.base_url}/tickets/{ticket_id}/messages/{message_id}/media",
                headers=self.headers,
            )
        except httpx.HTTPError as e:
            logger.warning("Bedolaga: could not describe media of message %d: %s", message_id, e)
            return None

        if described.status_code != 200:
            return None

        media_url = (described.json() or {}).get("media_url")
        if not media_url:
            logger.info("Bedolaga: message %d has media the panel cannot serve", message_id)
            return None

        resolved = self.resolve_media_url(str(media_url))
        if resolved is None:
            return None

        try:
            async with self.http_client.stream("GET", resolved, headers=self.headers) as downloaded:
                if downloaded.status_code != 200:
                    return None

                declared_length = downloaded.headers.get("content-length")
                if declared_length is not None:
                    try:
                        if int(declared_length) > MAX_MEDIA_BYTES:
                            logger.warning(
                                "Bedolaga: media of message %d declares an oversized body (%s bytes)",
                                message_id,
                                declared_length,
                            )
                            return None
                    except ValueError:
                        logger.warning(
                            "Bedolaga: media of message %d returned invalid Content-Length %r",
                            message_id,
                            declared_length,
                        )

                content = bytearray()
                async for chunk in downloaded.aiter_bytes():
                    if len(content) + len(chunk) > MAX_MEDIA_BYTES:
                        logger.warning(
                            "Bedolaga: media of message %d exceeded %d bytes while streaming",
                            message_id,
                            MAX_MEDIA_BYTES,
                        )
                        return None
                    content.extend(chunk)

                if not content:
                    return None

                mime_type = downloaded.headers.get("content-type") or DEFAULT_MEDIA_MIME_TYPE
        except httpx.HTTPError as e:
            logger.warning("Bedolaga: could not download media of message %d: %s", message_id, e)
            return None

        return ImageAttachment(
            base64_image=base64.b64encode(content).decode("ascii"),
            mime_type=mime_type.split(";")[0].strip(),
        )
