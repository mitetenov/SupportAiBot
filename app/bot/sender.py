"""Single outbound path to Telegram: chunking, media, and errors that never escape."""

import base64
import binascii
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, FSInputFile, URLInputFile

from app.bedolaga.types import TicketMedia
from app.bot.formatting import markdown_to_telegram_html, split_telegram_html, strip_html_tags

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096
MAX_TELEGRAM_MEDIA_BYTES: int = 50 * 1024 * 1024
TICKET_MEDIA_DOWNLOAD_TIMEOUT_SECONDS: int = 120


def _ticket_media_method(media: TicketMedia) -> Literal["photo", "video", "document"]:
    """Select the Telegram Bot API method for streaming ticket media."""
    if media.media_type == "photo":
        return "photo"
    if media.media_type in ("video", "video_note"):
        if (
            media.mime_type == "video/mp4"
            or media.filename.lower().endswith(".mp4")
            or media.mime_type is None
        ):
            return "video"
    return "document"


def _is_entity_parse_error(exc: Exception) -> bool:
    """Check if exception is a Telegram API entity parse error."""
    if isinstance(exc, TelegramBadRequest):
        msg = str(exc).lower()
        return "can't parse entities" in msg or "parse entities" in msg or "entity" in msg
    msg = str(exc).lower()
    return "can't parse entities" in msg or "parse entities" in msg


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
        # Telegram hands back a file_id for anything it has stored. Sending that
        # instead of the bytes turns every repeat of the same illustration into
        # one ordinary API call, so a picture is uploaded once per process.
        self._file_ids: dict[str, str] = {}

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

        formatted = markdown_to_telegram_html(text)
        chunks = self.split(formatted or "")

        for index, chunk in enumerate(chunks):
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
            }
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id
            if reply_to_message_id is not None and index == 0:
                kwargs["reply_to_message_id"] = reply_to_message_id
            if disable_notification:
                kwargs["disable_notification"] = True

            try:
                await self.bot.send_message(**kwargs)
            except Exception as e:
                if _is_entity_parse_error(e):
                    logger.warning(
                        "Telegram failed to parse HTML in message to %s (%s), falling back to plain text",
                        chat_id,
                        e,
                    )
                    try:
                        kwargs["text"] = strip_html_tags(chunk)
                        kwargs["parse_mode"] = None
                        await self.bot.send_message(**kwargs)
                    except Exception as plain_err:
                        logger.error("Failed to send message to %s: %s", chat_id, plain_err)
                else:
                    logger.error("Failed to send message to %s: %s", chat_id, e)

    async def send_photo(
        self,
        chat_id: int,
        path: Path,
        message_thread_id: int | None = None,
    ) -> int | None:
        """Send a picture from disk, returning the new message ID or None.

        An illustration is an extra on top of an answer that has already been
        delivered, so every way this can fail — the file missing from the image,
        a flood wait, the user having blocked the bot — is logged and swallowed.
        """
        key = str(path)
        photo: str | FSInputFile
        cached = self._file_ids.get(key)

        if cached is not None:
            photo = cached
        else:
            if not path.is_file():
                logger.warning("Illustration %s is not in the image — skipping it", path)
                return None
            photo = FSInputFile(path)

        kwargs: dict[str, Any] = {"chat_id": chat_id, "photo": photo}
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id

        try:
            result = await self.bot.send_photo(**kwargs)
        except Exception as e:
            logger.error("Failed to send photo %s to %s: %s", path, chat_id, e)
            return None

        if cached is None:
            self._remember_file_id(key, result)
        return getattr(result, "message_id", None)

    async def send_photo_bytes(
        self,
        chat_id: int,
        base64_image: str,
        mime_type: str,
        message_thread_id: int | None = None,
        caption: str | None = None,
    ) -> int | None:
        """Upload an in-memory picture, returning its Telegram message ID."""
        try:
            content = base64.b64decode(base64_image, validate=True)
        except ValueError, binascii.Error:
            logger.warning("Refusing an invalid base64 picture for chat %s", chat_id)
            return None
        if not content:
            logger.warning("Refusing an empty picture for chat %s", chat_id)
            return None

        extension = {
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }.get(mime_type.lower(), "jpg")
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": BufferedInputFile(content, filename=f"ticket-photo.{extension}"),
        }
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        if caption:
            kwargs["caption"] = caption

        try:
            result = await self.bot.send_photo(**kwargs)
        except Exception as e:
            logger.warning("Failed to upload ticket photo to %s: %s", chat_id, e)
            return None
        return getattr(result, "message_id", None)

    async def send_ticket_media(
        self,
        chat_id: int,
        media: TicketMedia,
        message_thread_id: int | None = None,
        caption: str | None = None,
    ) -> int | None:
        """Stream ticket media directly to Telegram via URLInputFile."""
        if media.file_size is not None and media.file_size > MAX_TELEGRAM_MEDIA_BYTES:
            logger.warning(
                "Rejecting oversized ticket media %s (%d bytes, limit %d bytes)",
                media.filename,
                media.file_size,
                MAX_TELEGRAM_MEDIA_BYTES,
            )
            return None

        def _create_input_file() -> URLInputFile:
            return URLInputFile(
                media.media_url,
                headers=dict(media.download_headers),
                filename=media.filename,
                timeout=TICKET_MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
            )

        method = _ticket_media_method(media)
        kwargs: dict[str, Any] = {"chat_id": chat_id}
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        if caption:
            kwargs["caption"] = caption

        try:
            if method == "photo":
                result = await self.bot.send_photo(photo=_create_input_file(), **kwargs)
            elif method == "video":
                try:
                    result = await self.bot.send_video(
                        video=_create_input_file(),
                        supports_streaming=True,
                        **kwargs,
                    )
                except TelegramBadRequest as e:
                    logger.warning(
                        "Telegram rejected video %s for chat %s (%s), falling back to send_document",
                        media.filename,
                        chat_id,
                        e,
                    )
                    result = await self.bot.send_document(
                        document=_create_input_file(),
                        **kwargs,
                    )
            else:
                result = await self.bot.send_document(document=_create_input_file(), **kwargs)

            return getattr(result, "message_id", None)
        except Exception as e:
            logger.warning(
                "Failed to send ticket media %s (type %s) to %s: %s",
                media.filename,
                media.media_type,
                chat_id,
                e,
            )
            return None

    def _remember_file_id(self, key: str, result: Any) -> None:
        """Keep the file_id Telegram assigned to a freshly uploaded picture."""
        sizes = getattr(result, "photo", None) or []
        file_id = getattr(sizes[-1], "file_id", None) if sizes else None
        if file_id:
            self._file_ids[key] = str(file_id)

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
        """Break text into Telegram-sized chunks, preserving balanced HTML tags."""
        return split_telegram_html(text, MAX_MESSAGE_LENGTH)
