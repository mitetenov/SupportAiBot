"""Single outbound path to Telegram: chunking, and errors that never escape."""

import logging
from collections.abc import Sequence
from typing import Any

from aiogram import Bot

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096


class TelegramMessageSender:
    """Every outbound message goes through here.

    Two things callers must not have to think about:

    * Telegram rejects anything over 4096 characters. A verbatim FAQ answer or a
      long ``/gaps`` listing crosses that line, so text is split on newlines
      before it is sent.
    * A send can fail for reasons that say nothing about the request being
      handled — the user blocked the bot, a flood wait, a deleted reply target.
      Those are logged and swallowed: losing the reply must not also lose the
      forward to the support topic that follows it.
    """

    MAX_MESSAGE_LENGTH: int = MAX_MESSAGE_LENGTH

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(
        self,
        chat_id: int,
        text: str | None,
        message_thread_id: int | None = None,
        disable_notification: bool = False,
    ) -> None:
        """Send text to a chat, or to one topic thread within it."""
        await self._send_chunks(
            chat_id,
            text,
            message_thread_id=message_thread_id,
            disable_notification=disable_notification,
        )

    async def send_to_topic(self, chat_id: int, topic_id: int, text: str | None) -> None:
        """Send text into a forum topic thread."""
        await self._send_chunks(chat_id, text, message_thread_id=topic_id)

    async def send_reply(
        self,
        chat_id: int,
        reply_to_message_id: int,
        text: str | None,
        message_thread_id: int | None = None,
    ) -> None:
        """Send text as a reply. Only the first chunk carries the reply link."""
        await self._send_chunks(
            chat_id,
            text,
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    async def _send_chunks(
        self,
        chat_id: int,
        text: str | None,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        disable_notification: bool = False,
    ) -> None:
        if text is None or not text.strip():
            return

        for index, chunk in enumerate(self.split(text)):
            kwargs: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id
            if reply_to_message_id is not None and index == 0:
                kwargs["reply_to_message_id"] = reply_to_message_id
            if disable_notification:
                kwargs["disable_notification"] = True

            try:
                await self.bot.send_message(**kwargs)
            except Exception as e:
                logger.error("Failed to send message to %s: %s", chat_id, e)

    async def copy_message(
        self,
        chat_id: int,
        from_chat_id: int,
        message_id: int,
        message_thread_id: int | None = None,
    ) -> int | None:
        """Copy a message, returning the new message ID or None when it failed."""
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
        }
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id

        try:
            result = await self.bot.copy_message(**kwargs)
        except Exception as e:
            logger.warning("Failed to copy message %d to %s: %s", message_id, chat_id, e)
            return None
        return getattr(result, "message_id", None)

    async def set_reaction(
        self,
        chat_id: int,
        message_id: int,
        reactions: Sequence[Any] | None,
    ) -> None:
        """Mirror reactions onto a message. An empty sequence clears them."""
        try:
            await self.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=list(reactions) if reactions else [],
            )
        except Exception as e:
            logger.error(
                "Error setting reaction on message %d in chat %s: %s", message_id, chat_id, e
            )

    @staticmethod
    def split(text: str) -> list[str]:
        """Break text into Telegram-sized chunks, preferring newline boundaries."""
        if not text:
            return [""]
        if len(text) <= MAX_MESSAGE_LENGTH:
            return [text]

        chunks: list[str] = []
        offset = 0
        while offset < len(text):
            end = min(offset + MAX_MESSAGE_LENGTH, len(text))
            if end < len(text):
                break_at = text.rfind("\n", offset, end)
                if break_at <= offset:
                    break_at = end
                end = break_at
            chunks.append(text[offset:end])
            offset = end
            if offset < len(text) and text[offset] == "\n":
                offset += 1
        return chunks
