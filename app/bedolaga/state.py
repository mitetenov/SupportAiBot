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

    Both ids come off the same row, and a turn needs both: one to know whether
    the newest user message is already answered, one to recognise the bot's own
    reply among the ticket's admin messages. Fetching them together keeps a
    turn to a single read.
    """

    #: The user's message the bot has answered up through.
    last_answered_message_id: int = 0
    #: The bot's own most recent reply. 0 means it has never replied here.
    last_bot_reply_message_id: int = 0

    def already_answered(self, message_id: int) -> bool:
        """True when this message, or a later one, has already been answered."""
        return self.last_answered_message_id >= message_id

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
        )

    async def record_reply(
        self,
        ticket_id: int,
        bot_reply_message_id: int,
        answered_message_id: int | None = None,
    ) -> None:
        """Record a reply the bot just posted into this ticket.

        `answered_message_id` is the user's message that reply answers, when it
        answers one. A hand-over line ("describe the problem in words") answers
        nothing — that user message stays the bot's to take on the next turn —
        but the reply itself must still be recorded, or the sweep after it
        would read the bot's own message as an operator stepping in.
        """
        async with self.db_manager.session() as session:
            if answered_message_id is None:
                row = await session.get(BedolagaTicketState, ticket_id)
                answered_message_id = row.last_answered_message_id if row is not None else 0
            await session.merge(
                BedolagaTicketState(
                    ticket_id=ticket_id,
                    last_answered_message_id=answered_message_id,
                    last_bot_reply_message_id=bot_reply_message_id,
                    updated_at=datetime.now(UTC),
                )
            )
