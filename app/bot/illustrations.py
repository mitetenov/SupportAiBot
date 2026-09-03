"""Sends the screenshot for an FAQ instruction used in the answer, once per conversation."""

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

    async def send_first(
        self,
        chat_id: int,
        user_id: int,
        faq_context: Any,
        delivered_answer: str,
    ) -> int | None:
        """Send the screenshot used by the delivered FAQ answer, returning its message id.

        Retrieval supplies candidate instructions to the model, not proof that it
        used one.  In particular, a personal-data tool can produce the final
        answer while an only loosely related FAQ remains the top search hit.
        Although the prompt allows concise summaries of FAQ instructions, illustration
        matching remains conservative: the screenshot is sent only when the top hit's
        full instruction text is present in the delivered answer.  A summarized or
        adapted answer may omit the optional illustration.

        Sent at most once per conversation: several entries name the same

        picture, because pressing the two buttons is the opening step of every
        connection answer, and a user working through a problem would otherwise
        be handed the same screenshot on every turn.
        """
        results = getattr(faq_context, "results", None) or []
        if not results:
            return None

        top_hit = results[0]
        name = getattr(top_hit, "image", None)
        if not name or self.conversation_state.was_illustration_sent(user_id, name):
            return None

        faq_answer = getattr(top_hit, "answer", None)
        if not self._contains_instruction(delivered_answer, faq_answer):
            return None

        path = faq_image_path(name)
        if path is None:
            return None

        message_id = await self.sender.send_photo(chat_id, path)
        if message_id is not None:
            self.conversation_state.record_illustration_sent(user_id, name)
        return message_id

    @staticmethod
    def _contains_instruction(delivered_answer: str, faq_answer: Any) -> bool:
        """Return whether the delivered text contains the retrieved instruction verbatim.

        Whitespace and case are presentation details and do not change whether
        the instruction was used.  Requiring the complete instruction favours a
        missing optional picture over attaching an unrelated one when the model
        summarizes or adapts the answer.
        """

        if not isinstance(faq_answer, str) or not faq_answer.strip():
            return False

        normalized_delivery = " ".join(delivered_answer.casefold().split())
        normalized_instruction = " ".join(faq_answer.casefold().split())
        return normalized_instruction in normalized_delivery
