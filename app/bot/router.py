"""Main aiogram Router: user text/photos/commands, support topic forwarding, and reaction sync."""

import logging
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.types import Message, MessageReactionUpdated
from sqlalchemy import select

from app.bot.buffer import BufferedMessage, UserMessageBuffer
from app.bot.command_handler import SupportCommandHandler
from app.bot.conversation_state import ConversationState
from app.bot.forwarder import SupportGroupForwarder
from app.bot.photo_downloader import PhotoDownloader
from app.bot.pipeline import UserMessagePipeline
from app.constants import get_message
from app.llm.base import LlmClient
from app.rag.knowledge_gaps import KnowledgeGapService
from app.storage.chat_history import ChatHistoryService
from app.storage.database import DatabaseSessionManager
from app.storage.models import MessageMapping, TopicMapping, User

logger = logging.getLogger(__name__)


async def ensure_user_info(db_manager: DatabaseSessionManager, user: Any) -> None:
    """Upsert Telegram user profile details in the database."""
    if user is None:
        return
    user_id = getattr(user, "id", None)
    if user_id is None:
        return
    try:
        async with db_manager.session() as session:
            db_user = await session.get(User, user_id)
            if db_user is None:
                db_user = User(telegram_id=user_id)
                session.add(db_user)
            db_user.username = getattr(user, "username", None)
            db_user.first_name = getattr(user, "first_name", None)
            db_user.last_name = getattr(user, "last_name", None)
            db_user.updated_at = datetime.now(UTC)
    except Exception as e:
        logger.debug("Failed to record user profile for %s: %s", user_id, e)


def setup_router(
    bot: Bot,
    llm_client: LlmClient,
    forwarder: SupportGroupForwarder,
    db_manager: DatabaseSessionManager,
    chat_history_service: ChatHistoryService,
    knowledge_gap_service: KnowledgeGapService,
    command_handler: SupportCommandHandler,
    photo_downloader: PhotoDownloader,
    message_buffer: UserMessageBuffer,
    pipeline: UserMessagePipeline,
    conversation_state: ConversationState,
    support_group_chat_id: int,
) -> Router:
    """Build and configure the primary aiogram Router."""
    router = Router(name="vpn_support_bot")

    # 1. Reaction synchronization
    @router.message_reaction()
    async def handle_reaction(reaction: MessageReactionUpdated) -> None:
        chat_id = reaction.chat.id
        message_id = reaction.message_id
        new_reactions = reaction.new_reaction or []

        try:
            async with db_manager.session() as session:
                if chat_id == support_group_chat_id:
                    stmt = select(MessageMapping).where(
                        MessageMapping.topic_message_id == message_id
                    )
                    res = await session.execute(stmt)
                    mapping = res.scalar_one_or_none()
                    if mapping is not None:
                        await bot.set_message_reaction(
                            chat_id=mapping.user_chat_id,
                            message_id=mapping.user_message_id,
                            reaction=new_reactions,
                        )
                else:
                    stmt = select(MessageMapping).where(
                        MessageMapping.user_chat_id == chat_id,
                        MessageMapping.user_message_id == message_id,
                    )
                    res = await session.execute(stmt)
                    mapping = res.scalar_one_or_none()
                    if mapping is not None:
                        await bot.set_message_reaction(
                            chat_id=support_group_chat_id,
                            message_id=mapping.topic_message_id,
                            reaction=new_reactions,
                        )
        except Exception as e:
            logger.debug("Failed to sync message reaction: %s", e)

    # 2. Support group operator messages
    @router.message(F.chat.id == support_group_chat_id)
    async def handle_support_group_message(message: Message) -> None:
        if message.from_user and message.from_user.is_bot:
            return

        topic_id = message.message_thread_id
        if topic_id is None:
            return

        async with db_manager.session() as session:
            stmt = select(TopicMapping).where(TopicMapping.topic_id == topic_id)
            res = await session.execute(stmt)
            mapping = res.scalar_one_or_none()

        if mapping is None:
            logger.debug("No user mapping found for support topic %d", topic_id)
            return

        user_id = mapping.user_id
        text = (message.text or message.caption or "").strip()

        if not text:
            # Media without text: copy to user
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=support_group_chat_id,
                    message_id=message.message_id,
                )
            except Exception as e:
                logger.warning("Failed to copy support media to user %d: %s", user_id, e)
                await bot.send_message(
                    chat_id=user_id,
                    text=get_message("support.fallback.media"),
                )
        else:
            replied_to = message.reply_to_message
            if replied_to is not None:
                async with db_manager.session() as session:
                    stmt = select(MessageMapping).where(
                        MessageMapping.topic_message_id == replied_to.message_id,
                        MessageMapping.topic_id == topic_id,
                    )
                    res = await session.execute(stmt)
                    msg_mapping = res.scalar_one_or_none()

                if msg_mapping is not None:
                    await bot.send_message(
                        chat_id=msg_mapping.user_chat_id,
                        reply_to_message_id=msg_mapping.user_message_id,
                        text=text,
                    )
                else:
                    await bot.send_message(
                        chat_id=user_id,
                        text=get_message("support.operator.prefix", text),
                    )
            else:
                await bot.send_message(
                    chat_id=user_id,
                    text=get_message("support.operator.prefix", text),
                )

        # Confirm delivery with text reply in topic
        try:
            await bot.send_message(
                chat_id=support_group_chat_id,
                message_thread_id=topic_id,
                reply_to_message_id=message.message_id,
                text=get_message("support.sent"),
            )
        except Exception as e:
            logger.warning("Failed to confirm delivery in topic %d: %s", topic_id, e)

        conversation_state.record_operator_reply(user_id)

    # 3. Direct user messages
    @router.message()
    async def handle_user_message(message: Message) -> None:
        if message.from_user and message.from_user.is_bot:
            return

        chat_id = message.chat.id
        user = message.from_user
        if user is None:
            return

        user_id = user.id
        await ensure_user_info(db_manager, user)

        text = (message.text or "").strip()

        # Handle slash commands
        if text and command_handler.is_command(text):
            cmd = text.split()[0]
            if cmd == "/start":
                await chat_history_service.clear(user_id)
                conversation_state.clear(user_id)
                await bot.send_message(chat_id=chat_id, text=get_message("bot.start.welcome"))
                return
            if cmd == "/help":
                await command_handler.send_help(chat_id)
                return
            if cmd == "/operator":
                last = conversation_state.last_query(user_id)
                if last is not None:
                    await knowledge_gap_service.evaluate_operator_request(
                        last.text,
                        user_id,
                        last.faq_context_or_empty(),
                    )
                await bot.send_message(chat_id=chat_id, text=get_message("bot.operator.transfer"))
                await forwarder.forward_to_support(
                    chat_id,
                    [message.message_id],
                    user,
                    get_message("support.operator.request"),
                    True,
                )
                return

            handled = await command_handler.handle_admin_command(chat_id, user_id, text)
            if not handled:
                await command_handler.send_unknown_command(chat_id)
            return

        # Plain text
        if text:
            buffered = BufferedMessage.from_text(message, text)
            message_buffer.submit(
                user_id,
                buffered,
                pipeline.handle,
            )
            return

        # Photos
        if message.photo:
            if not llm_client.supports_images():
                await bot.send_message(chat_id=chat_id, text=get_message("bot.photo.notsupported"))
                await forwarder.forward_to_support(
                    chat_id,
                    [message.message_id],
                    user,
                    get_message("support.media.received"),
                    False,
                )
                return

            result = await photo_downloader.download(message.photo)
            if not result.is_success():
                key = result.error_message_key or "bot.photo.error"
                await bot.send_message(chat_id=chat_id, text=get_message(key))
                return

            caption = (message.caption or "").strip()
            prompt = caption if caption else get_message("bot.photo.default.prompt")
            buffered = BufferedMessage(
                message=message,
                text=prompt,
                base64_image=result.base64_image,
                mime_type=result.mime_type,
            )
            message_buffer.submit(
                user_id,
                buffered,
                pipeline.handle,
            )
            return

        # Unsupported media (voice notes, videos, stickers, files, documents)
        await bot.send_message(chat_id=chat_id, text=get_message("bot.media.unsupported"))
        await forwarder.forward_to_support(
            chat_id,
            [message.message_id],
            user,
            get_message("support.media.received"),
            False,
        )

    return router
