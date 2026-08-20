"""Sends the screenshot named by the best-matching FAQ entry, at most once per conversation."""

import logging
from typing import Any

from app.bot.conversation_state import ConversationState
from app.bot.sender import TelegramMessageSender
from app.constants import faq_image_path

logger = logging.getLogger(__name__)


class IllustrationSender:
    """Decides whether a retrieval result comes with a picture, and delivers it."""

    def __init__(
        self, sender: TelegramMessageSender, conversation_state: ConversationState
    ) -> None:
        self.sender = sender
        self.conversation_state = conversation_state

    async def send_first(self, chat_id: int, user_id: int, faq_context: Any) -> int | None:
        """Send the screenshot named by the top FAQ hit, returning the message id it got.

        The entry that came back first is the one the answer was most likely
        built from, so its picture is the one to show. Getting that wrong costs
        a stray screenshot under a correct answer, which is why this is decided
        from the retrieval result rather than by asking the model to mark it —
        a marker would cost tokens on every request instead.

        Sent at most once per conversation: several entries name the same
        picture, because pressing the two buttons is the opening step of every
        connection answer, and a user working through a problem would otherwise
        be handed the same screenshot on every turn.
        """
        results = getattr(faq_context, "results", None) or []
        if not results:
            return None

        name = getattr(results[0], "image", None)
        if not name or self.conversation_state.was_illustration_sent(user_id, name):
            return None

        path = faq_image_path(name)
        if path is None:
            return None

        message_id = await self.sender.send_photo(chat_id, path)
        if message_id is not None:
            self.conversation_state.record_illustration_sent(user_id, name)
        return message_id
