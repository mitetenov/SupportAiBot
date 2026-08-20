"""Error reporting and alert dispatching to support supergroup."""

import logging

from aiogram import Bot

from app.constants import get_message

logger = logging.getLogger(__name__)


class AdminNotifier:
    """Dispatches diagnostic failure alerts to the administrators' support supergroup."""

    MAX_ERROR_LENGTH: int = 2000

    def __init__(self, bot: Bot, support_group_chat_id: int) -> None:
        self.bot = bot
        self.support_group_chat_id = support_group_chat_id

    async def notify_error(
        self,
        context: str,
        user_id: int | None = None,
        error: Exception | None = None,
    ) -> None:
        """Send a silent diagnostic alert to the support group."""
        raw_msg = str(error) if error is not None else "null"
        error_message = (
            raw_msg[: self.MAX_ERROR_LENGTH] if len(raw_msg) > self.MAX_ERROR_LENGTH else raw_msg
        )

        lines = [get_message("admin.error.prefix"), context]
        if user_id is not None:
            lines.append(f"User: {user_id}")
        lines.append("")
        lines.append(error_message)

        text = "\n".join(lines)
        try:
            await self.bot.send_message(
                chat_id=self.support_group_chat_id,
                text=text,
                disable_notification=True,
            )
        except Exception as e:
            logger.warning("Failed to send admin error notification: %s", e)
