"""One turn of a Bedolaga ticket conversation: read it, answer it, write it back."""

import asyncio
import logging
import time
from dataclasses import dataclass

from app.bedolaga.client import BedolagaClient
from app.bedolaga.state import TicketProgress, TicketStateStore
from app.bedolaga.types import ImageAttachment, Ticket, TicketMessage
from app.bot.admin_notifier import AdminNotifier
from app.bot.conversation_state import ConversationState
from app.bot.forwarder import SupportGroupForwarder
from app.bot.keyed_lock import KeyedLock
from app.bot.rate_limiter import UserRateLimiter
from app.constants import get_message
from app.llm.base import LlmClient, LlmReply
from app.llm.escalation import EscalationPolicy
from app.rag.knowledge_gaps import KnowledgeGapService

logger = logging.getLogger(__name__)

#: How long a ticket waits before its reply is attempted again, after the first
#: failure. Doubles per consecutive failure up to the ceiling below.
REPLY_BACKOFF_BASE_SECONDS: float = 60.0

#: The longest a ticket is left alone after repeated reply failures. Half an
#: hour is short enough that a fixed API key resumes work on its own, and long
#: enough that a permanently broken one costs one model call per ticket per
#: half hour instead of one per ticket per minute.
REPLY_BACKOFF_MAX_SECONDS: float = 1800.0

#: Doubling past this many failures is already at the ceiling; not counting
#: higher keeps the shift from growing an unbounded integer.
_MAX_COUNTED_FAILURES: int = 16

#: How many tickets one bot answers at a time when nobody configured it.
DEFAULT_MAX_CONCURRENT_TICKETS: int = 5


@dataclass
class _ReplyBackoff:
    """A ticket whose reply keeps failing, and when to try it again."""

    failures: int
    retry_at: float


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
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_TICKETS,
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
        # ticket id -> the message id whose suppression notice already went to
        # the topic. A suppressed ticket stays open with the user's message
        # last, so every sweep for the next half hour reads it again; without
        # this the operator's topic fills with the same notice dozens of times.
        # In memory on purpose: a restart costs at most one duplicate notice.
        self._suppressed: dict[int, int] = {}
        # ticket id -> when its reply may be tried again. A reply that cannot
        # land (a key without write scope, a `/reply` endpoint answering 500)
        # leaves the ticket open, so the next sweep pays for a whole model turn
        # to hit the same wall — every minute, for every affected ticket,
        # forever. In memory like the above: a restart is a free retry.
        self._reply_backoff: dict[int, _ReplyBackoff] = {}
        # One sweep can bring back a hundred tickets, and each turn holds a
        # connection from the pool the LLM and embedding providers share. Left
        # uncapped, a backlog starves plain Telegram messages of connections
        # and walks straight into the provider's rate limit.
        self._slots = asyncio.Semaphore(max(1, max_concurrent))

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
            self._reply_backoff.pop(ticket_id, None)
            return

        last = ticket.last_message
        progress = await self.state.progress(ticket.id)
        if last is None or progress.already_answered(last.id):
            self._reply_backoff.pop(ticket_id, None)
            return

        if self.backing_off(ticket.id):
            # Everything below this line costs money or noise, and the last
            # attempt proved the answer has nowhere to go. No admin alert
            # either: the retry when the window ends will raise one if it
            # fails again, and one alert per half hour is the point.
            return

        user_key = await self.user_key(ticket)
        if user_key is None:
            # The panel would not say who this is. Guessing means filing the
            # turn under a synthetic cabinet key, and that key outlives the
            # blip: a forum topic, a chat history and every future Remnawave
            # lookup for this person hang off it. Nothing is marked, so the
            # sweep a minute from now asks again.
            logger.warning(
                "Bedolaga ticket %d: the panel did not resolve user %d, leaving it for the "
                "next sweep",
                ticket.id,
                ticket.user_id,
            )
            return

        question = ticket.question

        if self.a_human_is_on_it(ticket, progress, user_key):
            # Somebody is already holding this conversation; a bot answer in
            # the ticket would talk over them. Nothing is marked answered — the
            # bot must pick this ticket up once they are done with it.
            if self._suppressed.get(ticket.id) != last.id:
                self._suppressed[ticket.id] = last.id
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

        # Everything above is a cheap decision not to work, and a burst of
        # tickets that are all going to be skipped must not queue up for a
        # semaphore slot to find that out. Everything below reaches the panel
        # or the model, so it is what the cap is for.
        async with self._slots:
            await self.answer_now(ticket, last, user_key, question)

    async def answer_now(
        self, ticket: Ticket, last: TicketMessage, user_key: int, question: str
    ) -> None:
        """Ask the model about this ticket and write the answer back.

        Split out of `_answer` so the concurrency cap wraps exactly the part
        that costs a connection and a model call, and nothing that does not.
        """
        attachment = await self.attachment_for(ticket)
        if not question.strip() and attachment is None:
            # There is genuinely nothing to ask about: no text anywhere in the
            # ticket and no picture the model can read. Sending an empty prompt
            # gets a plausible invention back, and `client.reply` would publish
            # it to the user as support's answer. Nothing is marked answered,
            # so the user's next message is still the bot's to take.
            await self.hand_over(ticket, user_key)
            return

        reply = await self.ask_model(question, user_key, attachment)
        answer = EscalationPolicy.strip_marker(reply.text) or get_message("bedolaga.llm.empty")
        escalate = EscalationPolicy.model_requested_escalation(
            reply.text
        ) or EscalationPolicy.user_requests_human(question)

        posted = answer + get_message("bedolaga.escalation.note") if escalate else answer
        reply_message_id = await self.client.reply(ticket.id, posted)
        if reply_message_id is None:
            await self.reply_failed(ticket.id, user_key)
            return

        self._reply_backoff.pop(ticket.id, None)
        await self.state.record_reply(ticket.id, reply_message_id, answered_message_id=last.id)
        self._suppressed.pop(ticket.id, None)
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

    def a_human_is_on_it(self, ticket: Ticket, progress: TicketProgress, user_key: int) -> bool:
        """True when somebody other than this bot is answering this person.

        Two ways to find that out, one meaning. The operator may be replying in
        Telegram, which the bot is told about directly; or they may be typing
        into Bedolaga's own admin UI, which nothing tells the bot about at all
        — there the only evidence is an admin message in the ticket that the
        bot did not write. Every reply in a ticket is an admin message, its own
        included, which is why the id of its own last reply is recorded: it is
        the only way to tell its handwriting from a human's.
        """
        if self.conversation_state.is_operator_recently_active(user_key):
            return True
        return any(
            message.is_from_admin and progress.someone_else_wrote(message.id)
            for message in ticket.messages
        )

    def backing_off(self, ticket_id: int) -> bool:
        """True while this ticket is waiting out a run of failed replies."""
        entry = self._reply_backoff.get(ticket_id)
        if entry is None or time.monotonic() >= entry.retry_at:
            return False
        logger.info(
            "Bedolaga ticket %d is backing off after %d failed repl(ies), %.0fs to go",
            ticket_id,
            entry.failures,
            entry.retry_at - time.monotonic(),
        )
        return True

    async def reply_failed(self, ticket_id: int, user_key: int) -> None:
        """Hold this ticket back for a while, and say so once.

        The answer is already paid for and thrown away. Without this the ticket
        stays open, the next sweep buys another one, and the whole thing repeats
        every minute for as long as the panel keeps refusing.
        """
        entry = self._reply_backoff.get(ticket_id)
        failures = min(entry.failures + 1 if entry is not None else 1, _MAX_COUNTED_FAILURES)
        delay = min(REPLY_BACKOFF_BASE_SECONDS * 2 ** (failures - 1), REPLY_BACKOFF_MAX_SECONDS)
        self._reply_backoff[ticket_id] = _ReplyBackoff(
            failures=failures,
            retry_at=time.monotonic() + delay,
        )
        logger.warning(
            "Bedolaga ticket %d: reply failed %d time(s), next attempt in %.0fs",
            ticket_id,
            failures,
            delay,
        )
        await self.admin_notifier.notify_error(
            get_message("bedolaga.reply.failed", ticket_id),
            user_id=user_key,
        )

    async def attachment_for(self, ticket: Ticket) -> ImageAttachment | None:
        """The screenshot this turn can show the model, when there is one.

        None is every reason the model will see no picture, answered in one
        place: no attachment at all, an attachment that is not a photo (voice,
        video, a document), a text-only provider, and a panel that could not
        serve the file. The caller needs that single answer twice — once to
        decide whether an empty question leaves anything to ask about, and once
        to pick which model call to make.
        """
        last = ticket.last_message
        if last is None or not last.has_media:
            return None
        if (last.media_type or "") != "photo":
            logger.info(
                "Bedolaga ticket %d: message %d carries %s, which the bot does not read",
                ticket.id,
                last.id,
                last.media_type or "an attachment of unknown type",
            )
            return None
        if not self.llm_client.supports_images():
            return None
        return await self.client.download_media(ticket.id, last.id)

    async def ask_model(
        self, question: str, user_key: int, attachment: ImageAttachment | None
    ) -> LlmReply:
        """Ask the model about this ticket, with the screenshot when there is one."""
        if attachment is None:
            return await self.llm_client.chat(question, user_key)

        prompt = question.strip() or get_message("bot.photo.default.prompt")
        return await self.llm_client.chat_with_image(
            prompt,
            user_key,
            attachment.base64_image,
            attachment.mime_type,
        )

    async def hand_over(self, ticket: Ticket, user_key: int) -> None:
        """Answer a ticket with nothing in it by asking for words, and call a human.

        Deliberately not routed through the model and deliberately not marked
        answered: the bot has been given nothing to work with, and the honest
        reply is a fixed line rather than whatever a model invents from an
        empty prompt. The mirror escalates so an operator, who can open the
        attachment the bot cannot, sees the ticket in the topic.
        """
        reply_message_id = await self.client.reply(
            ticket.id, get_message("bedolaga.nothing.to.answer")
        )
        if reply_message_id is None:
            # Cheaper to fail than the path above — no model call — but it
            # loops the same way, so it waits out the same backoff.
            await self.reply_failed(ticket.id, user_key)
            return

        self._reply_backoff.pop(ticket.id, None)
        # Still not marked answered: the user's message is the bot's to take
        # once they add words to it. The bot's own message is recorded all the
        # same, or the next turn would read it as an operator stepping in.
        await self.state.record_reply(ticket.id, reply_message_id)

        await self.mirror(
            ticket,
            user_key,
            get_message("bedolaga.nothing.mirror", ticket.id, ticket.title),
            escalate=True,
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

    async def user_key(self, ticket: Ticket) -> int | None:
        """The id this ticket's conversation is kept under, or None to try later.

        A Telegram id is what the rest of the bot keys on — chat history, FAQ
        follow-ups and every Remnawave lookup. A cabinet account registered by
        email has none, so it gets its panel id with the sign flipped: unique
        per person, never colliding with a real Telegram id, and finding
        nothing in Remnawave, which is exactly right — we cannot prove who
        that person is.

        None is not a third kind of person, it is "the panel did not answer".
        The negative key is permanent in practice, so it may only be minted
        when the panel actually said this account has no Telegram — never
        because a request timed out at the wrong moment.
        """
        lookup = await self.client.resolve_telegram_id(ticket.user_id)
        if not lookup.known:
            return None
        return lookup.telegram_id or -ticket.user_id
