"""The sweep that catches the ticket events a webhook never delivered."""

import logging

from app.bedolaga.client import DEFAULT_LIST_LIMIT, BedolagaClient
from app.bedolaga.pipeline import TicketAnswerer

logger = logging.getLogger(__name__)


class TicketPoller:
    """Schedules every ticket that is still waiting for support.

    Bedolaga's webhook delivery has no retries: one timeout and that ticket
    would sit unanswered forever. Scheduling is cheap and idempotent — a ticket
    already answered is dropped by the answerer after a single read.
    """

    def __init__(
        self,
        client: BedolagaClient,
        answerer: TicketAnswerer,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> None:
        self.client = client
        self.answerer = answerer
        self.limit = limit

    async def sweep(self) -> int:
        """Schedule the open tickets. Returns how many were scheduled."""
        try:
            ticket_ids = await self.client.list_awaiting_ticket_ids(self.limit)
        except Exception as e:
            # The scheduler logs and retries on the next tick; nothing is lost.
            logger.warning("Bedolaga ticket sweep failed: %s", e)
            return 0

        for ticket_id in ticket_ids:
            self.answerer.schedule(ticket_id)

        if ticket_ids:
            logger.info("Bedolaga sweep scheduled %d ticket(s)", len(ticket_ids))
        return len(ticket_ids)
