"""Answers a batch of user messages: rate limiting, model invocation, forwarding, and gap accounting."""

import logging
from typing import Any

from app.bot.buffer import MessageBatch
from app.bot.conversation_state import ConversationState
from app.bot.forwarder import SupportGroupForwarder
from app.bot.keyed_lock import KeyedLock
from app.bot.rate_limiter import UserRateLimiter
from app.bot.sender import TelegramMessageSender
from app.bot.typing import TypingIndicator
from app.constants import get_message
from app.llm.base import LlmClient, LlmProcessingException
from app.llm.escalation import EscalationPolicy
from app.rag.knowledge_gaps import KnowledgeGapService

logger = logging.getLogger(__name__)


class UserMessagePipeline:
    """Orchestrates processing of a coalesced MessageBatch."""

    MAX_ERROR_FORWARD_LENGTH: int = 3000

    def __init__(
        self,
        llm_client: LlmClient,
        sender: TelegramMessageSender,
        forwarder: SupportGroupForwarder,
        rate_limiter: UserRateLimiter,
        knowledge_gap_service: KnowledgeGapService,
        conversation_state: ConversationState,
        typing_indicator: TypingIndicator,
    ) -> None:
        self.llm_client = llm_client
        self.sender = sender
        self.forwarder = forwarder
        self.rate_limiter = rate_limiter
        self.knowledge_gap_service = knowledge_gap_service
        self.conversation_state = conversation_state
        self.typing_indicator = typing_indicator
        # One turn per user at a time. Two batches answered concurrently for the
        # same person interleave their writes to the chat history, and the model
        # sees a conversation neither of them actually had.
        self._turns = KeyedLock()

    async def handle(self, batch: MessageBatch) -> None:
        """Process an incoming coalesced message batch, one turn per user."""
        chat_id = self._chat_id(batch)
        user_id = getattr(batch.user, "id", None) or chat_id
        if chat_id is None or user_id is None:
            logger.warning("Dropping a batch with neither a chat nor a user id")
            return

        async with self._turns.hold(user_id):
            await self._handle_turn(batch, chat_id, int(user_id))

    @staticmethod
    def _chat_id(batch: MessageBatch) -> int | None:
        """The chat this batch came from, or None when the message has no chat."""
        chat_id = getattr(getattr(batch.last_message, "chat", None), "id", None)
        return int(chat_id) if chat_id is not None else None

    async def _handle_turn(self, batch: MessageBatch, chat_id: int, user_id: int) -> None:
        user = batch.user
        text = batch.text

        if self.conversation_state.is_operator_recently_active(user_id):
            await self.forwarder.forward_to_support(
                chat_id,
                batch.message_ids,
                user,
                get_message("support.ai.suppressed"),
                False,
            )
            return

        if not self.rate_limiter.try_acquire(user_id):
            await self.sender.send(chat_id, get_message("bot.ratelimit.wait"))
            await self.forwarder.forward_to_support(
                chat_id,
                batch.message_ids,
                user,
                get_message("support.ratelimited"),
                True,
            )
            return

        session = self.typing_indicator.start(chat_id)

        try:
            if batch.base64_image is not None:
                reply = await self.llm_client.chat_with_image(
                    text,
                    user_id,
                    batch.base64_image,
                    batch.mime_type,
                )
            else:
                reply = await self.llm_client.chat(text, user_id)

            self.conversation_state.record_query(user_id, text, reply.faq_context)

            response = EscalationPolicy.strip_marker(reply.text)
            if not response:
                response = get_message("bot.llm.empty")

            await self.sender.send(chat_id, response)

            escalate = EscalationPolicy.model_requested_escalation(
                reply.text
            ) or EscalationPolicy.user_requests_human(text)

            await self.forwarder.forward_to_support(
                chat_id,
                batch.message_ids,
                user,
                response,
                escalate,
            )
            # batch.user_text, not text: a captionless screenshot carries a prompt
            # we wrote, and reporting it back as the user's question fills /gaps
            # with a phrase nobody asked and no FAQ can ever match. With nothing
            # the user wrote there is no question to record at all.
            if batch.user_text.strip():
                await self.knowledge_gap_service.evaluate(
                    batch.user_text,
                    user_id,
                    reply.text,
                    reply.faq_context,
                )

        except LlmProcessingException as e:
            logger.error("LLM error processing message from user %d: %s", user_id, e)
            await self.report_failure(batch, user, e.user_friendly_message, e, chat_id)
        except Exception as e:
            logger.error("Error processing message from user %d: %s", user_id, e, exc_info=True)
            await self.report_failure(batch, user, get_message("bot.llm.error"), e, chat_id)
        finally:
            session.close()

    async def report_failure(
        self,
        batch: MessageBatch,
        user: Any,
        user_visible_message: str,
        cause: Exception,
        chat_id: int,
    ) -> None:
        """Inform user of failure and forward error details to support topic."""
        await self.sender.send(chat_id, user_visible_message)
        await self.forwarder.forward_error_to_topic(
            user,
            batch.text,
            user_visible_message,
            self.extract_error_message(cause),
        )

    def extract_error_message(self, cause: Exception) -> str:
        """Format and truncate technical error string for support group topic."""
        msg = str(cause)
        if len(msg) > self.MAX_ERROR_FORWARD_LENGTH:
            msg = msg[: self.MAX_ERROR_FORWARD_LENGTH] + "..."
        return f"Bot: {msg}" if msg else f"Bot: {cause.__class__.__name__}"
