"""The sweep that catches the ticket events a webhook never delivered."""

import logging

from app.bedolaga.client import DEFAULT_LIST_LIMIT, BedolagaClient
from app.bedolaga.pipeline import TicketAnswerer
from app.bedolaga.state import TicketStateStore
from app.logging_config import TRACE

logger = logging.getLogger(__name__)


class TicketPoller:
    """Schedules tickets awaiting support and answered tickets with pending media.

    Bedolaga's webhook delivery has no retries: one timeout and that ticket
    would sit unanswered forever. Media delivery has its own durable pending
    watermark, because a ticket can become answered before its topic recovers.
    """

    def __init__(
        self,
        client: BedolagaClient,
        answerer: TicketAnswerer,
        state: TicketStateStore,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> None:
        self.client = client
        self.answerer = answerer
        self.state = state
        self.limit = limit

    async def sweep(self) -> int:
        """Schedule open tickets and pending media retries, returning the unique count."""
        ticket_ids: list[int] = []
        try:
            ticket_ids.extend(await self.client.list_awaiting_ticket_ids(self.limit))
        except Exception as e:
            logger.error("Bedolaga ticket sweep failed", exc_info=True)
            logger.log(TRACE, "Bedolaga ticket sweep failed: %s", e)

        try:
            pending_media_ids = await self.state.pending_media_ticket_ids(self.limit)
        except Exception as e:
            logger.error("Bedolaga pending-media sweep failed", exc_info=True)
            logger.log(TRACE, "Bedolaga pending-media sweep failed: %s", e)
            pending_media_ids = []

        ticket_ids = list(dict.fromkeys([*ticket_ids, *pending_media_ids]))

        for ticket_id in ticket_ids:
            self.answerer.schedule(ticket_id)

        if ticket_ids:
            logger.info("Bedolaga sweep scheduled %d ticket(s)", len(ticket_ids))
        return len(ticket_ids)
