"""Delivery of human operator replies from Telegram topics into Bedolaga tickets."""

import logging

from app.bedolaga.client import BedolagaClient, PostedTicketReply
from app.bedolaga.state import TicketStateStore
from app.bot.conversation_state import ConversationState

logger = logging.getLogger(__name__)


class TicketOperatorRelay:
    """Writes operator text/photos into a ticket and records human ownership."""

    def __init__(
        self,
        client: BedolagaClient,
        state: TicketStateStore,
        conversation_state: ConversationState,
    ) -> None:
        self.client = client
        self.state = state
        self.conversation_state = conversation_state

    async def reply_text(self, ticket_id: int, user_key: int, text: str) -> bool:
        """Post operator text to the active ticket."""
        try:
            posted = await self.client.reply(ticket_id, text)
        except Exception as e:
            logger.error("Bedolaga ticket %d: operator text delivery failed: %s", ticket_id, e)
            return False
        return await self._record_delivery(ticket_id, user_key, posted)

    async def reply_photo(
        self,
        ticket_id: int,
        user_key: int,
        base64_image: str,
        mime_type: str,
        caption: str,
    ) -> bool:
        """Upload and attach an operator photo to the active ticket."""
        try:
            posted = await self.client.reply_with_photo(
                ticket_id,
                caption,
                base64_image,
                mime_type,
            )
        except Exception as e:
            logger.error("Bedolaga ticket %d: operator photo delivery failed: %s", ticket_id, e)
            return False
        return await self._record_delivery(ticket_id, user_key, posted)

    async def _record_delivery(
        self,
        ticket_id: int,
        user_key: int,
        posted: PostedTicketReply | None,
    ) -> bool:
        if posted is None:
            return False
        if posted.message_id is not None:
            try:
                await self.state.record_human_reply(ticket_id, posted.message_id)
            except Exception as e:
                # The message already landed. A state write must not turn that
                # success into a false delivery failure and tempt a duplicate.
                logger.warning(
                    "Bedolaga ticket %d: operator reply landed but state recording failed: %s",
                    ticket_id,
                    e,
                )
        self.conversation_state.record_operator_reply(user_key)
        return True
