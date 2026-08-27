"""One turn of a Bedolaga ticket conversation: read it, answer it, write it back."""

import asyncio
import logging
import time
from dataclasses import dataclass

from app.bedolaga.client import MAX_REPLY_LENGTH, BedolagaClient
from app.bedolaga.state import TicketProgress, TicketStateStore
from app.bedolaga.types import OPEN_STATUSES, ImageAttachment, Ticket, TicketMedia, TicketMessage
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
        self._active_tickets: set[int] = set()
        self._pending_tickets: set[int] = set()
        self._pending_rerun: set[int] = set()
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
        # Bounded concurrency over the entire ticket turn (including panel GETs
        # and model calls). Left uncapped, a backlog starves plain Telegram
        # messages of connections and walks straight into provider rate limits.
        self._slots = asyncio.Semaphore(max(1, max_concurrent))

    def schedule(self, ticket_id: int) -> None:
        """Answer this ticket in the background.

        Deduplicates in-flight and queued tasks: multiple schedule calls for the
        same ticket create at most 1 active execution and 1 pending rerun, not
        an unbounded set of tasks.
        """
        if ticket_id in self._active_tickets or ticket_id in self._pending_tickets:
            self._pending_rerun.add(ticket_id)
            return

        self._pending_tickets.add(ticket_id)
        task = asyncio.create_task(
            self._process_ticket(ticket_id), name=f"bedolaga-ticket-{ticket_id}"
        )
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    async def _process_ticket(self, ticket_id: int) -> None:
        """Process a ticket, repeating if rerun was requested while in flight."""
        self._pending_tickets.discard(ticket_id)
        self._active_tickets.add(ticket_id)
        try:
            while True:
                self._pending_rerun.discard(ticket_id)
                await self.handle(ticket_id)
                if ticket_id not in self._pending_rerun:
                    break
        finally:
            self._active_tickets.discard(ticket_id)

    async def drain(self) -> None:
        """Wait for the turns already in flight — used on shutdown."""
        if self._in_flight:
            await asyncio.gather(*tuple(self._in_flight), return_exceptions=True)

    async def handle(self, ticket_id: int) -> None:
        """Answer one ticket, one turn at a time, never raising to the caller."""
        async with self._slots, self._tickets.hold(ticket_id):
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
        if ticket is None:
            self._reply_backoff.pop(ticket_id, None)
            self._suppressed.pop(ticket_id, None)
            return

        progress = await self.state.progress(ticket.id)
        last = self.unanswered_user_message(ticket, progress)
        if last is None:
            self._reply_backoff.pop(ticket_id, None)
            self._suppressed.pop(ticket_id, None)
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

        question = ticket.question_for(last)

        if await self.a_human_is_on_it(ticket, progress, user_key):
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
                    source_message=last,
                )
            return

        if not self.rate_limiter.try_acquire(user_key):
            # Nothing is recorded, so the next sweep answers this message once
            # the window has passed.
            logger.info("Bedolaga ticket %d is rate limited for user %d", ticket.id, user_key)
            return

        await self.answer_now(ticket, last, user_key, question)

    async def answer_now(
        self, ticket: Ticket, last: TicketMessage, user_key: int, question: str
    ) -> None:
        """Ask the model about this ticket and write the answer back.

        Re-reads the ticket immediately before posting reply to prevent stale
        writes when user sends a new message or operator intervenes during LLM generation.
        """
        media = await self.media_for(ticket, last)
        attachment = await self.image_attachment_for(media)
        if not question.strip() and attachment is None:
            # There is genuinely nothing to ask about: no text anywhere in the
            # ticket and no picture the model can read. Sending an empty prompt
            # gets a plausible invention back, and `client.reply` would publish
            # it to the user as support's answer. The fixed hand-over line is
            # recorded against this empty turn; a later message has a larger
            # id and is still the bot's to take.
            await self.hand_over(ticket, last, user_key, media=media)
            return

        reply = await self.ask_model(question, user_key, attachment)

        answer = EscalationPolicy.strip_marker(reply.text) or get_message("bedolaga.llm.empty")
        escalate = EscalationPolicy.model_requested_escalation(
            reply.text
        ) or EscalationPolicy.user_requests_human(question)

        escalation_note = get_message("bedolaga.escalation.note") if escalate else ""
        max_answer_len = MAX_REPLY_LENGTH - len(escalation_note)
        truncated_answer = answer[:max_answer_len]
        posted = truncated_answer + escalation_note

        # Stale-check before posting reply:
        # Re-fetch ticket to ensure no operator replied, ticket was not closed,
        # and no new user messages arrived during the LLM call.
        fresh_ticket = await self.client.get_ticket(ticket.id)
        if fresh_ticket is None:
            logger.info(
                "Bedolaga ticket %d: could not verify state before reply, dropping stale answer",
                ticket.id,
            )
            return

        snapshot_last_id = ticket.last_message.id if ticket.last_message is not None else last.id
        new_messages = [
            message for message in fresh_ticket.messages if message.id > snapshot_last_id
        ]
        status_is_compatible = fresh_ticket.status == ticket.status or (
            fresh_ticket.status in OPEN_STATUSES and ticket.status in OPEN_STATUSES
        )
        if not status_is_compatible:
            logger.info(
                "Bedolaga ticket %d: status changed from %s to %s, dropping stale answer",
                ticket.id,
                ticket.status,
                fresh_ticket.status,
            )
            self._reply_backoff.pop(ticket.id, None)
            self._suppressed.pop(ticket.id, None)
            return

        if new_messages:
            if any(message.is_from_admin for message in new_messages):
                logger.info(
                    "Bedolaga ticket %d: operator replied during LLM call, dropping stale answer",
                    ticket.id,
                )
                self._reply_backoff.pop(ticket.id, None)
                return
            # User sent new message(s) during model generation. Drop stale answer and schedule rerun.
            logger.info(
                "Bedolaga ticket %d: new user message %r arrived during LLM call, scheduling rerun",
                ticket.id,
                fresh_ticket.last_user_message.id if fresh_ticket.last_user_message else None,
            )
            self._pending_rerun.add(ticket.id)
            return

        posted_reply = await self.client.reply(ticket.id, posted)
        if posted_reply is None:
            await self.reply_failed(ticket.id, user_key)
            return

        self._reply_backoff.pop(ticket.id, None)
        await self.state.record_reply(
            ticket.id, posted_reply.message_id, answered_message_id=last.id
        )
        self._suppressed.pop(ticket.id, None)
        self.conversation_state.record_query(user_key, question, reply.faq_context)

        await self.schedule_newer_user_message(ticket.id, last.id)

        if escalate:
            success = await self.client.set_priority(ticket.id, "high")
            if not success:
                logger.warning("Bedolaga ticket %d: set_priority to high failed", ticket.id)

        await self.mirror(
            ticket,
            user_key,
            get_message("bedolaga.mirror", ticket.id, ticket.title, question, truncated_answer),
            escalate=escalate,
            source_message=last,
            media=media,
        )


        if question.strip():
            await self.knowledge_gap_service.evaluate(
                question,
                user_key,
                reply.text,
                reply.faq_context,
            )

    async def a_human_is_on_it(
        self, ticket: Ticket, progress: TicketProgress, user_key: int
    ) -> bool:
        """True when somebody other than this bot is answering this person.

        Two sources of human activity:
        1. Telegram support topic activity via ConversationState.
        2. Bedolaga panel admin replies with id > last_bot_reply_message_id.

        When a human reply is detected in the panel, it is recorded in the DB with a timestamp
        (for persistence across restarts) and ConversationState. Suppression lasts for
        operator_suppression_window (30 minutes). After 30 minutes of inactivity, the bot
        resumes answering new user messages in this ticket.
        """
        human_messages = [
            message
            for message in ticket.messages
            if message.is_from_admin and progress.someone_else_wrote(message.id)
        ]
        if not human_messages:
            return self.conversation_state.is_operator_recently_active(user_key)

        latest_human_message_id = max(message.id for message in human_messages)
        if progress.human_reply_is_new(latest_human_message_id):
            await self.state.record_human_reply(ticket.id, latest_human_message_id)
            self.conversation_state.record_operator_reply(user_key)
            return True

        if self.conversation_state.is_operator_recently_active(user_key):
            return True

        window = self.conversation_state.operator_suppression_window
        return progress.is_human_recently_active(window)

    @staticmethod
    def unanswered_user_message(ticket: Ticket, progress: TicketProgress) -> TicketMessage | None:
        """The user turn still owed an answer, including one hidden by our reply.

        A user can write after the pre-POST check but before the POST lands. The
        bot reply then becomes the last admin message and flips the ticket to
        `answered`; the stored watermark is what proves the intervening user
        message was not part of that model turn.
        """
        candidate = ticket.last_user_message
        if candidate is None or progress.already_answered(candidate.id):
            return None
        if ticket.awaits_answer:
            return candidate
        if ticket.status == "answered" and progress.last_bot_reply_message_id > candidate.id:
            return candidate
        return None

    async def schedule_newer_user_message(self, ticket_id: int, answered_message_id: int) -> None:
        """Reconcile the narrow race between the final GET and the reply POST."""
        fresh_ticket = await self.client.get_ticket(ticket_id)
        if fresh_ticket is None:
            return
        latest_user = fresh_ticket.last_user_message
        if latest_user is not None and latest_user.id > answered_message_id:
            logger.info(
                "Bedolaga ticket %d: user message %d arrived while the reply was landing; rerunning",
                ticket_id,
                latest_user.id,
            )
            self._pending_rerun.add(ticket_id)

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

    async def media_for(self, ticket: Ticket, message: TicketMessage) -> TicketMedia | None:
        """Fetch media descriptor for an attachment in this turn."""
        if not message.has_media:
            return None
        return await self.client.describe_media(ticket.id, message.id, message.media_type)

    async def image_attachment_for(
        self,
        media: TicketMedia | None,
    ) -> ImageAttachment | None:
        """The screenshot this turn can show the model, when there is one."""
        if media is None or media.media_type != "photo" or not self.llm_client.supports_images():
            return None
        return await self.client.download_image(media)

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

    async def hand_over(
        self,
        ticket: Ticket,
        last: TicketMessage,
        user_key: int,
        media: TicketMedia | None = None,
    ) -> None:
        """Answer a ticket with nothing in it by asking for words, and call a human.

        Deliberately not routed through the model: the bot has been given
        nothing to work with, and the honest reply is a fixed line rather than
        whatever a model invents from an empty prompt. The empty turn is marked
        answered so every sweep does not send the same hand-over again; a later
        user message remains eligible. The mirror escalates so an operator, who
        can open the attachment the bot cannot, sees the ticket in the topic.
        """
        posted_reply = await self.client.reply(ticket.id, get_message("bedolaga.nothing.to.answer"))
        if posted_reply is None:
            # Cheaper to fail than the path above — no model call — but it
            # loops the same way, so it waits out the same backoff.
            await self.reply_failed(ticket.id, user_key)
            return

        self._reply_backoff.pop(ticket.id, None)
        # The fixed line is the answer to this empty turn. Marking its watermark
        # prevents it from being handed over repeatedly; a later user message
        # has a larger id and remains eligible as usual.
        await self.state.record_reply(
            ticket.id, posted_reply.message_id, answered_message_id=last.id
        )
        await self.schedule_newer_user_message(ticket.id, last.id)

        await self.mirror(
            ticket,
            user_key,
            get_message("bedolaga.nothing.mirror", ticket.id, ticket.title),
            escalate=True,
            source_message=last,
            media=media,
        )

    async def mirror(
        self,
        ticket: Ticket,
        user_key: int,
        text: str,
        escalate: bool,
        source_message: TicketMessage | None = None,
        media: TicketMedia | None = None,
    ) -> None:
        """Put this ticket turn into the user's forum topic.

        The answer is already delivered — by Bedolaga, into the ticket — so a
        support group that is down or misconfigured must not cost the user
        their reply. Every failure here stays here.
        """
        try:
            mirrored_media = media
            if (
                mirrored_media is None
                and source_message is not None
                and source_message.has_media
            ):
                mirrored_media = await self.media_for(ticket, source_message)

            await self.forwarder.forward_to_support(
                user_chat_id=user_key,
                user_message_ids=None,
                user=self.stand_in(ticket, user_key),
                bot_response=text,
                needs_escalation=escalate,
                ticket_id=ticket.id,
                ticket_media=mirrored_media,
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
