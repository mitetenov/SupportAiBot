"""One turn of a Bedolaga ticket conversation: read it, answer it, write it back."""

import asyncio
import logging
from dataclasses import dataclass

from app.bedolaga.client import BedolagaClient
from app.bedolaga.state import TicketStateStore
from app.bedolaga.types import Ticket
from app.bot.admin_notifier import AdminNotifier
from app.bot.conversation_state import ConversationState
from app.bot.forwarder import SupportGroupForwarder
from app.bot.keyed_lock import KeyedLock
from app.bot.rate_limiter import UserRateLimiter
from app.constants import get_message
from app.llm.base import LlmClient
from app.llm.escalation import EscalationPolicy
from app.rag.knowledge_gaps import KnowledgeGapService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TicketUser:
    """A stand-in for an aiogram user, for the code that forwards to a topic.

    SupportGroupForwarder only ever reads `id`, `username`, `first_name` and
    `last_name` off the sender, and a ticket has no aiogram update behind it.
    """

    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class TicketAnswerer:
    """Answers Bedolaga tickets with the model that answers Telegram messages."""

    def __init__(
        self,
        client: BedolagaClient,
        llm_client: LlmClient,
        state: TicketStateStore,
        rate_limiter: UserRateLimiter,
        admin_notifier: AdminNotifier,
        forwarder: SupportGroupForwarder,
        knowledge_gap_service: KnowledgeGapService,
        conversation_state: ConversationState,
    ) -> None:
        self.client = client
        self.llm_client = llm_client
        self.state = state
        self.rate_limiter = rate_limiter
        self.admin_notifier = admin_notifier
        self.forwarder = forwarder
        self.knowledge_gap_service = knowledge_gap_service
        self.conversation_state = conversation_state
        # One turn per ticket: a webhook and a poll sweep can both bring in the
        # same ticket a millisecond apart.
        self._tickets = KeyedLock()
        self._in_flight: set[asyncio.Task[None]] = set()

    def schedule(self, ticket_id: int) -> None:
        """Answer this ticket in the background.

        A webhook delivery has ten seconds before Bedolaga gives up on it, and
        a model turn takes longer than that — so the HTTP handler schedules and
        answers 200 immediately.
        """
        task = asyncio.create_task(self.handle(ticket_id), name=f"bedolaga-ticket-{ticket_id}")
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    async def drain(self) -> None:
        """Wait for the turns already in flight — used on shutdown."""
        if self._in_flight:
            await asyncio.gather(*tuple(self._in_flight), return_exceptions=True)

    async def handle(self, ticket_id: int) -> None:
        """Answer one ticket, one turn at a time, never raising to the caller."""
        async with self._tickets.hold(ticket_id):
            try:
                await self._answer(ticket_id)
            except Exception as e:
                logger.error("Failed to answer Bedolaga ticket %d: %s", ticket_id, e, exc_info=True)
                await self.admin_notifier.notify_error(
                    get_message("bedolaga.error.context", ticket_id),
                    error=e,
                )

    async def _answer(self, ticket_id: int) -> None:
        ticket = await self.client.get_ticket(ticket_id)
        if ticket is None or not ticket.awaits_answer:
            return

        last = ticket.last_message
        if last is None or await self.state.already_answered(ticket.id, last.id):
            return

        user_key = await self.user_key(ticket)
        question = ticket.question

        if self.conversation_state.is_operator_recently_active(user_key):
            # The operator is holding this conversation in Telegram; a bot
            # answer in the ticket would talk over them.
            await self.mirror(
                ticket,
                user_key,
                get_message("bedolaga.suppressed", ticket.id, ticket.title, question),
                escalate=True,
            )
            return

        if not self.rate_limiter.try_acquire(user_key):
            # Nothing is recorded, so the next sweep answers this message once
            # the window has passed.
            logger.info("Bedolaga ticket %d is rate limited for user %d", ticket.id, user_key)
            return

        reply = await self.llm_client.chat(question, user_key)
        answer = EscalationPolicy.strip_marker(reply.text) or get_message("bedolaga.llm.empty")
        escalate = EscalationPolicy.model_requested_escalation(
            reply.text
        ) or EscalationPolicy.user_requests_human(question)

        posted = answer + get_message("bedolaga.escalation.note") if escalate else answer
        if not await self.client.reply(ticket.id, posted):
            await self.admin_notifier.notify_error(
                get_message("bedolaga.reply.failed", ticket.id),
                user_id=user_key,
            )
            return

        await self.state.mark_answered(ticket.id, last.id)
        self.conversation_state.record_query(user_key, question, reply.faq_context)

        if escalate:
            await self.client.set_priority(ticket.id, "high")

        await self.mirror(
            ticket,
            user_key,
            get_message("bedolaga.mirror", ticket.id, ticket.title, question, answer),
            escalate=escalate,
        )

        if question.strip():
            await self.knowledge_gap_service.evaluate(
                question,
                user_key,
                reply.text,
                reply.faq_context,
            )

    async def mirror(self, ticket: Ticket, user_key: int, text: str, escalate: bool) -> None:
        """Put this ticket turn into the user's forum topic.

        The answer is already delivered — by Bedolaga, into the ticket — so a
        support group that is down or misconfigured must not cost the user
        their reply. Every failure here stays here.
        """
        try:
            await self.forwarder.forward_to_support(
                user_chat_id=user_key,
                user_message_ids=None,
                user=self.stand_in(ticket, user_key),
                bot_response=text,
                needs_escalation=escalate,
            )
        except Exception as e:
            logger.warning("Could not mirror Bedolaga ticket %d to the topic: %s", ticket.id, e)

    @staticmethod
    def stand_in(ticket: Ticket, user_key: int) -> TicketUser:
        """The sender the forwarder needs to find or name a topic.

        A Telegram user already has a topic under their own id. A cabinet-only
        account does not, so its topic is named after the panel account rather
        than the synthetic negative id nobody would recognise.
        """
        if user_key > 0:
            return TicketUser(id=user_key)
        return TicketUser(id=user_key, first_name=f"Кабинет #{ticket.user_id}")

    async def user_key(self, ticket: Ticket) -> int:
        """The id this ticket's conversation is kept under.

        A Telegram id is what the rest of the bot keys on — chat history, FAQ
        follow-ups and every Remnawave lookup. A cabinet account registered by
        email has none, so it gets its panel id with the sign flipped: unique
        per person, never colliding with a real Telegram id, and finding
        nothing in Remnawave, which is exactly right — we cannot prove who
        that person is.
        """
        telegram_id = await self.client.resolve_telegram_id(ticket.user_id)
        return telegram_id if telegram_id else -ticket.user_id
