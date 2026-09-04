"""The operator's /ask command: an answer from the model, delivered to the user as the bot."""

import logging
import re

from app.bot.conversation_state import ConversationState
from app.bot.illustrations import IllustrationSender
from app.bot.sender import TelegramMessageSender
from app.bot.typing import TypingIndicator
from app.constants import get_message
from app.llm.base import LlmClient
from app.llm.escalation import EscalationPolicy
from app.logging_config import TRACE
from app.logging_context import operation_context

logger = logging.getLogger(__name__)


class OperatorAskCommand:
    """Answers on the operator's behalf while a human is holding the conversation.

    An operator reply mutes the bot for half an hour, which also cuts the
    operator off from the knowledge base: to hand the user an FAQ article they
    would have to find and paste it themselves. `/ask <вопрос>` runs the normal
    answering path under the user's Telegram ID — FAQ retrieval, their chat
    history, their Remnawave data — and sends the result to the user as an
    ordinary bot message.
    """

    #: `/ask`, optionally addressed to the bot, and the query that follows.
    #: Anchored on a word boundary so `/asking` stays the operator's own text.
    PATTERN: re.Pattern[str] = re.compile(r"^/ask(?:@\w+)?(?:\s+|$)", re.IGNORECASE)

    MAX_ERROR_LENGTH: int = 500

    def __init__(
        self,
        llm_client: LlmClient,
        sender: TelegramMessageSender,
        conversation_state: ConversationState,
        typing_indicator: TypingIndicator,
        support_group_chat_id: int,
    ) -> None:
        self.llm_client = llm_client
        self.sender = sender
        self.conversation_state = conversation_state
        self.typing_indicator = typing_indicator
        self.support_group_chat_id = support_group_chat_id
        self.illustrations = IllustrationSender(sender, conversation_state)

    @classmethod
    def parse(cls, text: str | None) -> str | None:
        """The query this /ask carries, or None when the text is not the command.

        An empty string means the operator sent the command with nothing to ask,
        which is a different case from text that was never a command at all.
        """
        if not text:
            return None
        match = cls.PATTERN.match(text.strip())
        if match is None:
            return None
        return text.strip()[match.end() :].strip()

    async def handle(self, topic_id: int, user_id: int, query: str) -> None:
        """Answer the query as the bot would and deliver it to the user."""
        if not query:
            await self._notify_operator(topic_id, get_message("support.ask.usage"))
            return

        with operation_context():
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Operator /ask handling for user_id=%d, topic_id=%d: query=%s",
                    user_id,
                    topic_id,
                    query,
                )
            # The user's private chat is keyed by their own id, as everywhere else
            # the bot writes to them.
            session = self.typing_indicator.start(user_id)
            try:
                reply = await self.llm_client.chat(query, user_id)

                response = EscalationPolicy.strip_marker(reply.text)
                if not response:
                    response = get_message("bot.llm.empty")

                await self.sender.send(user_id, response)
                await self.illustrations.send_first(user_id, user_id, reply.faq_context, response)

                # The operator is still the one running this conversation, so the
                # bot stays out of the way for another suppression window.
                self.conversation_state.record_operator_reply(user_id)
                await self._notify_operator(topic_id, get_message("support.ask.header", response))
            except Exception as e:
                logger.error(
                    "Operator /ask failed (component=OperatorAskCommand, operation=handle, error_class=%s)",
                    type(e).__name__,
                )
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "Operator /ask failed for user %d: %s",
                        user_id,
                        e,
                        exc_info=True,
                    )
                await self._notify_operator(
                    topic_id, get_message("support.ask.error", self._describe(e))
                )
            finally:
                session.close()

    async def _notify_operator(self, topic_id: int, text: str) -> None:
        """Write back into the topic the command came from."""
        await self.sender.send_to_topic(self.support_group_chat_id, topic_id, text)

    def _describe(self, cause: Exception) -> str:
        """A short, bounded description of a failure for the topic."""
        message = str(cause) or cause.__class__.__name__
        if len(message) > self.MAX_ERROR_LENGTH:
            message = message[: self.MAX_ERROR_LENGTH] + "..."
        return message
