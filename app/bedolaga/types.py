"""The slice of the Bedolaga ticket API this bot reads, as plain data."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Statuses in which a ticket is still waiting for support. The bot writes
#: `open`, the cabinet writes `pending` — both mean the same thing to us.
OPEN_STATUSES: frozenset[str] = frozenset({"open", "pending"})


@dataclass(frozen=True)
class ImageAttachment:
    """A ticket screenshot, encoded the way the vision APIs want it."""

    base64_image: str
    mime_type: str


@dataclass(frozen=True)
class TelegramIdLookup:
    """What the panel said when asked for the Telegram id behind a panel user.

    `telegram_id is None` is a real answer — a cabinet account registered by
    email has no Telegram at all — but only when `known` is True. A panel that
    answered 502, or did not answer, knows nothing about this user either way,
    and the two outcomes must never collapse into one value: the caller turns
    "no Telegram id" into a synthetic identity that, once a forum topic and a
    chat history hang off it, cannot be taken back.
    """

    known: bool
    telegram_id: int | None = None


#: The panel could not be asked, or did not answer — ask again later.
TELEGRAM_ID_UNKNOWN: TelegramIdLookup = TelegramIdLookup(known=False)


@dataclass(frozen=True)
class TicketMessage:
    """One message inside a ticket."""

    id: int
    text: str
    is_from_admin: bool
    has_media: bool = False
    media_type: str | None = None


@dataclass(frozen=True)
class Ticket:
    """A ticket with its messages, oldest first — the order the API returns."""

    id: int
    user_id: int
    title: str
    status: str
    priority: str = "normal"
    messages: tuple[TicketMessage, ...] = ()

    @property
    def last_message(self) -> TicketMessage | None:
        """The most recent message, or None for a ticket that somehow has none."""
        return self.messages[-1] if self.messages else None

    @property
    def awaits_answer(self) -> bool:
        """True when the last word is the user's and the ticket is still open."""
        last = self.last_message
        return last is not None and not last.is_from_admin and self.status in OPEN_STATUSES

    @property
    def question(self) -> str:
        """What to ask the model.

        A ticket opened a minute ago is a title plus one message, and the title
        is usually where the actual problem is named ("Не работает оплата" +
        "уже третий раз"). Once the thread is running, the title is stale
        context the chat history already carries, so only the newest message
        counts.

        Unless that newest message has no text at all — a bare screenshot after
        "пришлите скриншот" is the commonest ticket there is. The title is the
        only thing left that names the problem, so it stands in whatever the
        length of the thread, rather than leaving nothing to ask about.
        """
        last = self.last_message
        text = last.text.strip() if last else ""
        title = self.title.strip()
        if not text:
            return title
        if len(self.messages) == 1 and title and title.lower() not in text.lower():
            return f"{title}\n\n{text}"
        return text


def message_from_payload(payload: Mapping[str, Any]) -> TicketMessage:
    """Build a TicketMessage from one element of the API's `messages` array."""
    return TicketMessage(
        id=int(payload.get("id") or 0),
        text=str(payload.get("message_text") or ""),
        is_from_admin=bool(payload.get("is_from_admin")),
        has_media=bool(payload.get("has_media")),
        media_type=payload.get("media_type") or None,
    )


def ticket_from_payload(payload: Mapping[str, Any]) -> Ticket:
    """Build a Ticket from the body of `GET /tickets/{id}`."""
    raw_messages = payload.get("messages") or []
    return Ticket(
        id=int(payload["id"]),
        user_id=int(payload.get("user_id") or 0),
        title=str(payload.get("title") or ""),
        status=str(payload.get("status") or ""),
        priority=str(payload.get("priority") or "normal"),
        messages=tuple(message_from_payload(item) for item in raw_messages),
    )
