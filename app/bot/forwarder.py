"""Copies user messages to topic, saves message mappings, and posts bot answers with escalation tags."""

import logging
from collections.abc import Sequence
from typing import Any

from aiogram import Bot

from app.bot.topic_manager import TopicManager
from app.constants import get_message
from app.storage.database import DatabaseSessionManager
from app.storage.models import MessageMapping

logger = logging.getLogger(__name__)


class SupportGroupForwarder:
    """Manages bidirectional message forwarding between users and their support forum topics."""

    SUPPORT_PREVIEW_MAX_LENGTH: int = 3500
    ERROR_USER_MSG_MAX_LENGTH: int = 300

    def __init__(
        self,
        bot: Bot,
        topic_manager: TopicManager,
        db_manager: DatabaseSessionManager,
        support_group_chat_id: int,
        admin_username: str | None = None,
    ) -> None:
        self.bot = bot
        self.topic_manager = topic_manager
        self.db_manager = db_manager
        self.support_group_chat_id = support_group_chat_id
        self.admin_username = admin_username.lstrip("@") if admin_username else ""

    async def forward_to_support(
        self,
        user_chat_id: int,
        user_message_ids: Sequence[int] | None,
        user: Any,
        bot_response: str,
        needs_escalation: bool,
    ) -> None:
        """Copies batch messages into user topic and appends bot response."""
        user_id = getattr(user, "id", user_chat_id)
        user_name = self.resolve_user_name(user)
        topic_id = await self.topic_manager.resolve_topic_id(user_id, user_name)

        if topic_id is None:
            logger.warning("Cannot forward to support group: no topic for user %s", user_id)
            return

        if not user_message_ids:
            await self._send_bot_response(topic_id, user_name, bot_response, needs_escalation)
            return

        # Recreate topic on first failure only
        ok = await self._forward_user_message(user_chat_id, user_message_ids[0], topic_id)
        if not ok:
            logger.warning("Failed to forward to topic %s, recreating for user %s", topic_id, user_id)
            topic_id = await self.topic_manager.recreate_stale_topic(user_id, user_name, topic_id)
            if topic_id is None:
                logger.error("Failed to recreate topic for user %s", user_id)
                return
            ok = await self._forward_user_message(user_chat_id, user_message_ids[0], topic_id)
            if not ok:
                logger.error("Still failed to forward after topic recreation for user %s", user_id)
                return

        for msg_id in user_message_ids[1:]:
            await self._forward_user_message(user_chat_id, msg_id, topic_id)

        await self._send_bot_response(topic_id, user_name, bot_response, needs_escalation)

    async def _forward_user_message(
        self,
        user_chat_id: int,
        user_message_id: int,
        topic_id: int,
    ) -> bool:
        """Copy single user message to topic and record mapping."""
        try:
            res = await self.bot.copy_message(
                chat_id=self.support_group_chat_id,
                from_chat_id=user_chat_id,
                message_id=user_message_id,
                message_thread_id=topic_id,
            )
            topic_msg_id = getattr(res, "message_id", None)
            if topic_msg_id is not None:
                async with self.db_manager.session() as session:
                    mapping = MessageMapping(
                        topic_message_id=topic_msg_id,
                        topic_id=topic_id,
                        user_chat_id=user_chat_id,
                        user_message_id=user_message_id,
                    )
                    session.add(mapping)
            return True
        except Exception as e:
            logger.warning("Error copying user message to topic %d: %s", topic_id, e)
            return False

    async def _send_bot_response(
        self,
        topic_id: int,
        user_name: str,
        bot_response: str,
        needs_escalation: bool,
    ) -> None:
        """Send formatted bot response summary to the support topic."""
        admin_tag = f"@{self.admin_username} " if needs_escalation and self.admin_username else ""
        header = f"{admin_tag}{get_message('admin.response.prefix')} {user_name}:\n\n"

        if len(bot_response) > self.SUPPORT_PREVIEW_MAX_LENGTH:
            truncated = (
                bot_response[: self.SUPPORT_PREVIEW_MAX_LENGTH]
                + "...\n\n"
                + get_message("admin.response.truncated")
            )
        else:
            truncated = bot_response

        await self.bot.send_message(
            chat_id=self.support_group_chat_id,
            message_thread_id=topic_id,
            text=header + truncated,
        )

    async def forward_error_to_topic(
        self,
        user: Any,
        user_message: str,
        user_visible_message: str,
        error_details: str,
    ) -> None:
        """Report processing failure into the user forum topic with admin escalation tag."""
        user_id = getattr(user, "id", None)
        user_name = self.resolve_user_name(user)
        topic_id = await self.topic_manager.resolve_topic_id(user_id, user_name)
        if topic_id is None:
            logger.warning("Cannot forward error to support group: no topic for user %s", user_id)
            return

        admin_tag = f"@{self.admin_username} " if self.admin_username else ""

        trunc_user_msg = (
            user_message[: self.ERROR_USER_MSG_MAX_LENGTH] + "..."
            if len(user_message) > self.ERROR_USER_MSG_MAX_LENGTH
            else user_message
        )

        msg1 = (
            f"{get_message('admin.error.prefix')} {user_name}: {trunc_user_msg}\n\n"
            f"Бот ответил:\n{user_visible_message}"
        )
        await self.bot.send_message(
            chat_id=self.support_group_chat_id,
            message_thread_id=topic_id,
            text=msg1,
        )

        trunc_err = (
            error_details[: self.SUPPORT_PREVIEW_MAX_LENGTH] + "..."
            if len(error_details) > self.SUPPORT_PREVIEW_MAX_LENGTH
            else error_details
        )
        msg2 = f"{admin_tag}{get_message('admin.error.details')}\n\n{trunc_err}"
        await self.bot.send_message(
            chat_id=self.support_group_chat_id,
            message_thread_id=topic_id,
            text=msg2,
        )

    def resolve_user_name(self, user: Any) -> str:
        """Format user display handle: @username, First Last, First, or User <id>."""
        if user is None:
            return "Unknown"

        username = getattr(user, "username", None)
        if username and str(username).strip():
            return f"@{str(username).strip()}"

        first = getattr(user, "first_name", "") or ""
        last = getattr(user, "last_name", "") or ""
        name = f"{first} {last}".strip()
        if name:
            return name

        user_id = getattr(user, "id", "Unknown")
        return f"User {user_id}"
