"""What this bot has already done on a Bedolaga ticket."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.storage.database import DatabaseSessionManager
from app.storage.models import BedolagaTicketState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TicketProgress:
    """One ticket's row, as the pipeline reads it.

    All watermarks come off the same row: one says which user turn was
    answered, one recognises the bot's own admin messages, and one makes a new
    human reply refresh the persisted suppression window exactly once.
    Fetching them together keeps a turn to a single read.
    """

    #: The user's message the bot has answered up through.
    last_answered_message_id: int = 0
    #: The bot's own most recent reply. 0 means it has never replied here.
    last_bot_reply_message_id: int = 0
    #: The newest admin message identified as a human's.
    last_human_reply_message_id: int = 0
    #: The newest user message whose media has already been mirrored to support topic.
    last_mirrored_media_message_id: int = 0
    #: When a human operator last replied in Bedolaga panel, or None if never recorded.
    last_human_reply_at: datetime | None = None

    def already_answered(self, message_id: int) -> bool:
        """True when this message, or a later one, has already been answered."""
        return self.last_answered_message_id >= message_id

    def media_already_mirrored(self, message_id: int) -> bool:
        """True when this message's media has already been mirrored to support."""
        return self.last_mirrored_media_message_id >= message_id

    def someone_else_wrote(self, admin_message_id: int) -> bool:
        """True when this admin message is newer than anything the bot wrote.

        A ticket the bot has never replied on gives nothing to compare against,
        so this deliberately stays false. The integration cannot distinguish a
        pre-existing human reply from its own history before it started
        recording ids; treating either as a human would permanently silence
        the first bot turn on every old thread. Once the bot has posted one
        reply, an admin message with a later id is unambiguously human.
        """
        return self.last_bot_reply_message_id > 0 and (
            admin_message_id > self.last_bot_reply_message_id
        )

    def is_human_recently_active(self, window_seconds: float, now: datetime | None = None) -> bool:
        """True when a human operator reply is within the suppression window."""
        if self.last_human_reply_at is None:
            return False
        current_time = now if now is not None else datetime.now(UTC)
        return (current_time - self.last_human_reply_at).total_seconds() < window_seconds

    def human_reply_is_new(self, message_id: int) -> bool:
        """True when this human reply has not refreshed the suppression TTL yet."""
        return message_id > self.last_human_reply_message_id


#: A ticket this bot has never touched.
NOTHING_DONE: TicketProgress = TicketProgress()


class TicketStateStore:
    """Reads and writes the one row per ticket that makes answering idempotent.

    Single-process by design: `progress` then `record_reply` is check-then-act,
    guarded only by the in-process `KeyedLock` in `TicketAnswerer`. Two bot
    instances against one database would answer the same ticket twice; the
    deployment runs one container.
    """

    def __init__(self, db_manager: DatabaseSessionManager) -> None:
        self.db_manager = db_manager

    async def progress(self, ticket_id: int) -> TicketProgress:
        """What the bot has already done on this ticket."""
        async with self.db_manager.session() as session:
            row = await session.get(BedolagaTicketState, ticket_id)
        if row is None:
            return NOTHING_DONE
        return TicketProgress(
            last_answered_message_id=row.last_answered_message_id or 0,
            # `or 0` because the column was added after the first rows could
            # have been written, and an unset value has to read as "never".
            last_bot_reply_message_id=row.last_bot_reply_message_id or 0,
            last_human_reply_message_id=row.last_human_reply_message_id or 0,
            last_mirrored_media_message_id=row.last_mirrored_media_message_id or 0,
            last_human_reply_at=row.last_human_reply_at,
        )

    async def record_mirrored_media(
        self,
        ticket_id: int,
        message_id: int,
    ) -> None:
        """Record that user media up through message_id has been mirrored."""
        now = datetime.now(UTC)
        async with self.db_manager.session() as session:
            row = await session.get(BedolagaTicketState, ticket_id)
            if row is None:
                await session.merge(
                    BedolagaTicketState(
                        ticket_id=ticket_id,
                        last_answered_message_id=0,
                        last_bot_reply_message_id=0,
                        last_human_reply_message_id=0,
                        last_mirrored_media_message_id=message_id,
                        last_human_reply_at=None,
                        updated_at=now,
                    )
                )
            else:
                row.last_mirrored_media_message_id = max(
                    row.last_mirrored_media_message_id or 0, message_id
                )
                row.updated_at = now
                await session.merge(row)

    async def record_human_reply(
        self,
        ticket_id: int,
        message_id: int,
        reply_at: datetime | None = None,
    ) -> None:
        """Record a human operator reply and refresh its suppression window."""
        now = reply_at or datetime.now(UTC)
        async with self.db_manager.session() as session:
            row = await session.get(BedolagaTicketState, ticket_id)
            if row is None:
                await session.merge(
                    BedolagaTicketState(
                        ticket_id=ticket_id,
                        last_answered_message_id=0,
                        last_bot_reply_message_id=0,
                        last_human_reply_message_id=message_id,
                        last_mirrored_media_message_id=0,
                        last_human_reply_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.last_human_reply_message_id = max(
                    row.last_human_reply_message_id or 0, message_id
                )
                row.last_human_reply_at = now
                row.updated_at = now
                await session.merge(row)

    async def record_reply(
        self,
        ticket_id: int,
        bot_reply_message_id: int | None,
        answered_message_id: int | None = None,
    ) -> None:
        """Record a reply the bot just posted into this ticket.

        `answered_message_id` is the user's message that reply answers. Callers
        may omit it only when recording a bot message that advances no user
        watermark. A missing bot reply id preserves the prior known watermark:
        the panel accepted the write, but its malformed response must not erase
        information already stored.
        """
        async with self.db_manager.session() as session:
            row = await session.get(BedolagaTicketState, ticket_id)
            if answered_message_id is None:
                answered_message_id = row.last_answered_message_id if row is not None else 0
            last_bot_reply_message_id = (
                bot_reply_message_id
                if bot_reply_message_id is not None
                else (row.last_bot_reply_message_id if row is not None else 0)
            )
            last_human_message_id = row.last_human_reply_message_id or 0 if row is not None else 0
            last_human_at = row.last_human_reply_at if row is not None else None
            last_mirrored_media = row.last_mirrored_media_message_id or 0 if row is not None else 0
            await session.merge(
                BedolagaTicketState(
                    ticket_id=ticket_id,
                    last_answered_message_id=answered_message_id,
                    last_bot_reply_message_id=last_bot_reply_message_id,
                    last_human_reply_message_id=last_human_message_id,
                    last_mirrored_media_message_id=last_mirrored_media,
                    last_human_reply_at=last_human_at,
                    updated_at=datetime.now(UTC),
                )
            )
