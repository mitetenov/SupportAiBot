from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiohttp import web

from app.bedolaga.relay import TicketOperatorRelay
from app.bot.maintenance import MaintenanceJob
from app.config import Settings, reveal

if TYPE_CHECKING:
    import httpx

    from app.bedolaga.pipeline import TicketAnswerer
    from app.bedolaga.poller import TicketPoller
    from app.bedolaga.webhook import BedolagaWebhookEndpoint
    from app.bot.admin_notifier import AdminNotifier
    from app.bot.conversation_state import ConversationState
    from app.bot.forwarder import SupportGroupForwarder
    from app.bot.rate_limiter import UserRateLimiter
    from app.llm.base import LlmClient
    from app.rag.knowledge_gaps import KnowledgeGapService
    from app.storage.database import DatabaseSessionManager

logger = logging.getLogger(__name__)

__all__ = ["TicketOperatorRelay", "TicketSupport", "create_ticket_support"]


@dataclass(frozen=True)
class TicketSupport:
    """Everything the ticket integration needs the application to hold on to."""

    answerer: TicketAnswerer
    poller: TicketPoller
    endpoint: BedolagaWebhookEndpoint
    operator_relay: TicketOperatorRelay
    webhook_path: str
    poll_interval_seconds: float

    def register_routes(self, app: web.Application) -> None:
        """Mount the webhook endpoint on the bot's HTTP server."""
        self.endpoint.register(app, self.webhook_path)

    def maintenance_job(self) -> MaintenanceJob:
        """The recurring sweep, in the shape MaintenanceScheduler runs."""
        return MaintenanceJob(
            name="bedolaga-ticket-sweep",
            interval_seconds=self.poll_interval_seconds,
            run=self.poller.sweep,
        )


def create_ticket_support(
    settings: Settings,
    http_client: httpx.AsyncClient,
    llm_client: LlmClient,
    db_manager: DatabaseSessionManager,
    forwarder: SupportGroupForwarder,
    admin_notifier: AdminNotifier,
    rate_limiter: UserRateLimiter,
    knowledge_gap_service: KnowledgeGapService,
    conversation_state: ConversationState,
) -> TicketSupport | None:
    """Assemble the Bedolaga ticket integration, or None when it is switched off."""
    if not settings.bedolaga_enabled:
        return None

    from app.bedolaga.client import BedolagaClient
    from app.bedolaga.pipeline import TicketAnswerer
    from app.bedolaga.poller import TicketPoller
    from app.bedolaga.relay import TicketOperatorRelay
    from app.bedolaga.state import TicketStateStore
    from app.bedolaga.webhook import BedolagaWebhookEndpoint

    client = BedolagaClient(
        base_url=settings.bedolaga_api_url,
        api_key=reveal(settings.bedolaga_api_key),
        http_client=http_client,
    )
    state = TicketStateStore(db_manager)

    answerer = TicketAnswerer(
        client=client,
        llm_client=llm_client,
        state=state,
        rate_limiter=rate_limiter,
        admin_notifier=admin_notifier,
        forwarder=forwarder,
        knowledge_gap_service=knowledge_gap_service,
        conversation_state=conversation_state,
        max_concurrent=settings.bedolaga_max_concurrent_tickets,
    )
    logger.info(
        "Bedolaga ticket integration enabled: %s, sweeping every %ds, %d ticket(s) at a time",
        settings.bedolaga_api_url,
        settings.bedolaga_poll_interval_seconds,
        settings.bedolaga_max_concurrent_tickets,
    )
    return TicketSupport(
        answerer=answerer,
        poller=TicketPoller(client=client, answerer=answerer, state=state),
        operator_relay=TicketOperatorRelay(
            client=client,
            state=state,
            conversation_state=conversation_state,
        ),
        endpoint=BedolagaWebhookEndpoint(
            answerer=answerer,
            secret=reveal(settings.bedolaga_webhook_secret),
        ),
        webhook_path=settings.bedolaga_webhook_path,
        poll_interval_seconds=float(settings.bedolaga_poll_interval_seconds),
    )
