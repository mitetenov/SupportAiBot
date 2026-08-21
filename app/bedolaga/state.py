"""Which ticket messages this bot has already answered."""

import logging
from datetime import UTC, datetime

from app.storage.database import DatabaseSessionManager
from app.storage.models import BedolagaTicketState

logger = logging.getLogger(__name__)


class TicketStateStore:
    """Reads and writes the one row per ticket that makes answering idempotent."""

    def __init__(self, db_manager: DatabaseSessionManager) -> None:
        self.db_manager = db_manager

    async def already_answered(self, ticket_id: int, message_id: int) -> bool:
        """True when this message, or a later one, has already been answered."""
        async with self.db_manager.session() as session:
            row = await session.get(BedolagaTicketState, ticket_id)
        return row is not None and row.last_answered_message_id >= message_id

    async def mark_answered(self, ticket_id: int, message_id: int) -> None:
        """Record that the ticket has been answered up to this message."""
        async with self.db_manager.session() as session:
            await session.merge(
                BedolagaTicketState(
                    ticket_id=ticket_id,
                    last_answered_message_id=message_id,
                    updated_at=datetime.now(UTC),
                )
            )
