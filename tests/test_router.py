"""Unit tests for the aiogram Router and update routing."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.types import (
    Chat,
    Message,
    MessageReactionUpdated,
    PhotoSize,
    ReactionTypeEmoji,
    Update,
    User,
)

from app.bot.command_handler import SupportCommandHandler
from app.bot.conversation_state import ConversationState
from app.bot.router import setup_router
from app.bot.sender import TelegramMessageSender
from app.storage.chat_history import ChatHistoryService
from app.storage.models import MessageMapping, TopicMapping
from app.storage.models import User as DbUser


class MockDatabaseSessionManager:
    def __init__(self):
        self.topic_mappings_by_topic: dict[int, TopicMapping] = {}
        self.message_mappings: list[MessageMapping] = []
        self.users: dict[int, DbUser] = {}

    @asynccontextmanager
    async def session(self):
        session_mock = MagicMock()

        def add_mock(obj):
            if isinstance(obj, TopicMapping):
                self.topic_mappings_by_topic[obj.topic_id] = obj
            elif isinstance(obj, MessageMapping):
                self.message_mappings.append(obj)
            elif isinstance(obj, DbUser):
                self.users[obj.telegram_id] = obj

        async def get_mock(model, key):
            if model == DbUser:
                return self.users.get(key)
            return None

        async def execute_mock(stmt, params=None):
            result_mock = MagicMock()
            stmt_str = str(stmt)
            if "topic_mappings" in stmt_str:
                for topic_id, mapping in self.topic_mappings_by_topic.items():
                    if str(topic_id) in stmt_str or any(
                        str(topic_id) in str(p) for p in (params or {}).values()
                    ):
                        result_mock.scalar_one_or_none.return_value = mapping
                        return result_mock
                result_mock.scalar_one_or_none.return_value = (
                    list(self.topic_mappings_by_topic.values())[0]
                    if self.topic_mappings_by_topic
                    else None
                )
                return result_mock

            if "message_mappings" in stmt_str:
                if "topic_message_id" in stmt_str:
                    for m in self.message_mappings:
                        if str(m.topic_message_id) in stmt_str or any(
                            str(m.topic_message_id) in str(p) for p in (params or {}).values()
                        ):
                            result_mock.scalar_one_or_none.return_value = m
                            return result_mock
                if "user_chat_id" in stmt_str and "user_message_id" in stmt_str:
                    for m in self.message_mappings:
                        if str(m.user_message_id) in stmt_str or any(
                            str(m.user_message_id) in str(p) for p in (params or {}).values()
                        ):
                            result_mock.scalar_one_or_none.return_value = m
                            return result_mock
                result_mock.scalar_one_or_none.return_value = (
                    self.message_mappings[0] if self.message_mappings else None
                )
                return result_mock

            return result_mock

        session_mock.add = add_mock
        session_mock.get = AsyncMock(side_effect=get_mock)
        session_mock.execute = AsyncMock(side_effect=execute_mock)
        session_mock.commit = AsyncMock()
        session_mock.rollback = AsyncMock()
        session_mock.close = AsyncMock()
        yield session_mock


@pytest.fixture
def mock_db():
    return MockDatabaseSessionManager()


@pytest.mark.asyncio
async def test_router_handles_start_command(mock_db):
    bot = MagicMock()
    bot.send_message = AsyncMock()

    chat_history = ChatHistoryService()
    conv_state = ConversationState()
    cmd_handler = SupportCommandHandler(bot, mock_db, MagicMock(), admin_telegram_ids={111})

    router = setup_router(
        sender=TelegramMessageSender(bot),
        llm_client=MagicMock(),
        forwarder=MagicMock(),
        db_manager=mock_db,
        chat_history_service=chat_history,
        knowledge_gap_service=MagicMock(),
        command_handler=cmd_handler,
        photo_downloader=MagicMock(),
        message_buffer=MagicMock(),
        pipeline=MagicMock(),
        conversation_state=conv_state,
        support_group_chat_id=-100123,
    )

    dp = Dispatcher()
    dp.include_router(router)

    user = User(id=100, is_bot=False, first_name="Test", username="testuser")
    chat = Chat(id=100, type="private")
    message = Message(message_id=1, date=123456, chat=chat, from_user=user, text="/start")
    update = Update(update_id=1, message=message)

    await dp.feed_update(bot, update)

    bot.send_message.assert_called_once()
    assert "Привет" in bot.send_message.call_args[1]["text"]


@pytest.mark.asyncio
async def test_router_handles_operator_command(mock_db):
    bot = MagicMock()
    bot.send_message = AsyncMock()

    forwarder = MagicMock()
    forwarder.forward_to_support = AsyncMock()

    gap_service = MagicMock()
    gap_service.evaluate_operator_request = AsyncMock()

    conv_state = ConversationState()
    conv_state.record_query(100, "мой вопрос")

    cmd_handler = SupportCommandHandler(bot, mock_db, gap_service, admin_telegram_ids={111})

    router = setup_router(
        sender=TelegramMessageSender(bot),
        llm_client=MagicMock(),
        forwarder=forwarder,
        db_manager=mock_db,
        chat_history_service=ChatHistoryService(),
        knowledge_gap_service=gap_service,
        command_handler=cmd_handler,
        photo_downloader=MagicMock(),
        message_buffer=MagicMock(),
        pipeline=MagicMock(),
        conversation_state=conv_state,
        support_group_chat_id=-100123,
    )

    dp = Dispatcher()
    dp.include_router(router)

    user = User(id=100, is_bot=False, first_name="Test", username="testuser")
    chat = Chat(id=100, type="private")
    message = Message(message_id=1, date=123456, chat=chat, from_user=user, text="/operator")
    update = Update(update_id=1, message=message)

    await dp.feed_update(bot, update)

    gap_service.evaluate_operator_request.assert_called_once()
    bot.send_message.assert_called_once()
    assert forwarder.forward_to_support.call_count == 1
    call_args = forwarder.forward_to_support.call_args[0]
    assert call_args[0] == 100
    assert call_args[1] == [1]
    assert call_args[2].id == 100
    assert call_args[3] == "Пользователь запросил живого оператора."
    assert call_args[4] is True


@pytest.mark.asyncio
async def test_router_support_group_operator_reply(mock_db):
    mock_db.topic_mappings_by_topic[42] = TopicMapping(
        user_id=100, topic_id=42, user_name="testuser"
    )
    mock_db.message_mappings.append(
        MessageMapping(
            topic_message_id=500,
            topic_id=42,
            user_chat_id=100,
            user_message_id=20,
        )
    )

    bot = MagicMock()
    bot.send_message = AsyncMock()

    conv_state = ConversationState()

    router = setup_router(
        sender=TelegramMessageSender(bot),
        llm_client=MagicMock(),
        forwarder=MagicMock(),
        db_manager=mock_db,
        chat_history_service=ChatHistoryService(),
        knowledge_gap_service=MagicMock(),
        command_handler=MagicMock(),
        photo_downloader=MagicMock(),
        message_buffer=MagicMock(),
        pipeline=MagicMock(),
        conversation_state=conv_state,
        support_group_chat_id=-100123,
    )

    dp = Dispatcher()
    dp.include_router(router)

    op_user = User(id=777, is_bot=False, first_name="Operator")
    group_chat = Chat(id=-100123, type="supergroup")
    replied_msg = Message(message_id=500, date=123450, chat=group_chat)
    op_msg = Message(
        message_id=501,
        date=123456,
        chat=group_chat,
        from_user=op_user,
        message_thread_id=42,
        text="Вот решение проблемы",
        reply_to_message=replied_msg,
    )
    update = Update(update_id=1, message=op_msg)

    await dp.feed_update(bot, update)

    assert bot.send_message.call_count == 2

    first_call = bot.send_message.call_args_list[0][1]
    assert first_call["chat_id"] == 100
    assert first_call["reply_to_message_id"] == 20
    assert first_call["text"] == "Вот решение проблемы"

    second_call = bot.send_message.call_args_list[1][1]
    assert second_call["chat_id"] == -100123
    assert second_call["reply_to_message_id"] == 501
    assert "Отправлено" in second_call["text"]

    assert conv_state.is_operator_recently_active(100)


@pytest.mark.asyncio
async def test_router_forwards_user_message_to_buffer(mock_db):
    bot = MagicMock()
    buffer = MagicMock()
    buffer.submit = MagicMock()

    router = setup_router(
        sender=TelegramMessageSender(bot),
        llm_client=MagicMock(),
        forwarder=MagicMock(),
        db_manager=mock_db,
        chat_history_service=ChatHistoryService(),
        knowledge_gap_service=MagicMock(),
        command_handler=SupportCommandHandler(bot, mock_db, MagicMock(), admin_telegram_ids=set()),
        photo_downloader=MagicMock(),
        message_buffer=buffer,
        pipeline=MagicMock(),
        conversation_state=ConversationState(),
        support_group_chat_id=-100123,
    )

    dp = Dispatcher()
    dp.include_router(router)

    user = User(id=100, is_bot=False, first_name="Test", username="testuser")
    chat = Chat(id=100, type="private")
    message = Message(message_id=10, date=123456, chat=chat, from_user=user, text="Обычный вопрос")
    update = Update(update_id=1, message=message)

    await dp.feed_update(bot, update)

    buffer.submit.assert_called_once()
    user_id_arg, buffered_msg_arg, sink_arg = buffer.submit.call_args[0]
    assert user_id_arg == 100
    assert buffered_msg_arg.text == "Обычный вопрос"


@pytest.mark.asyncio
async def test_router_unsupported_media_handling(mock_db):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    forwarder = MagicMock(forward_to_support=AsyncMock())

    router = setup_router(
        sender=TelegramMessageSender(bot),
        llm_client=MagicMock(),
        forwarder=forwarder,
        db_manager=mock_db,
        chat_history_service=ChatHistoryService(),
        knowledge_gap_service=MagicMock(),
        command_handler=SupportCommandHandler(bot, mock_db, MagicMock()),
        photo_downloader=MagicMock(),
        message_buffer=MagicMock(),
        pipeline=MagicMock(),
        conversation_state=ConversationState(),
        support_group_chat_id=-100123,
    )

    dp = Dispatcher()
    dp.include_router(router)

    user = User(id=100, is_bot=False, first_name="Test")
    chat = Chat(id=100, type="private")
    # Message without text and without photo (e.g. voice/sticker)
    message = Message(message_id=22, date=123456, chat=chat, from_user=user)
    update = Update(update_id=1, message=message)

    await dp.feed_update(bot, update)

    bot.send_message.assert_called_once()
    assert "Пока я работаю только с текстом" in bot.send_message.call_args[1]["text"]
    forwarder.forward_to_support.assert_called_once()


@pytest.mark.asyncio
async def test_router_photo_when_images_unsupported(mock_db):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    forwarder = MagicMock(forward_to_support=AsyncMock())
    llm_client = MagicMock(supports_images=MagicMock(return_value=False))

    router = setup_router(
        sender=TelegramMessageSender(bot),
        llm_client=llm_client,
        forwarder=forwarder,
        db_manager=mock_db,
        chat_history_service=ChatHistoryService(),
        knowledge_gap_service=MagicMock(),
        command_handler=SupportCommandHandler(bot, mock_db, MagicMock()),
        photo_downloader=MagicMock(),
        message_buffer=MagicMock(),
        pipeline=MagicMock(),
        conversation_state=ConversationState(),
        support_group_chat_id=-100123,
    )

    dp = Dispatcher()
    dp.include_router(router)

    user = User(id=100, is_bot=False, first_name="Test")
    chat = Chat(id=100, type="private")
    photo_sizes = [PhotoSize(file_id="f1", file_unique_id="u1", width=100, height=100)]
    message = Message(message_id=33, date=123456, chat=chat, from_user=user, photo=photo_sizes)
    update = Update(update_id=1, message=message)

    await dp.feed_update(bot, update)

    bot.send_message.assert_called_once()
    assert "не умею работать с изображениями" in bot.send_message.call_args[1]["text"]
    forwarder.forward_to_support.assert_called_once()


@pytest.mark.asyncio
async def test_router_reaction_sync_user_to_support(mock_db):
    mock_db.message_mappings.append(
        MessageMapping(
            topic_message_id=300,
            topic_id=42,
            user_chat_id=100,
            user_message_id=10,
        )
    )

    bot = MagicMock()
    bot.set_message_reaction = AsyncMock()

    router = setup_router(
        sender=TelegramMessageSender(bot),
        llm_client=MagicMock(),
        forwarder=MagicMock(),
        db_manager=mock_db,
        chat_history_service=ChatHistoryService(),
        knowledge_gap_service=MagicMock(),
        command_handler=SupportCommandHandler(bot, mock_db, MagicMock()),
        photo_downloader=MagicMock(),
        message_buffer=MagicMock(),
        pipeline=MagicMock(),
        conversation_state=ConversationState(),
        support_group_chat_id=-100123,
    )

    dp = Dispatcher()
    dp.include_router(router)

    chat = Chat(id=100, type="private")
    reaction_update = MessageReactionUpdated(
        chat=chat,
        message_id=10,
        date=123456,
        old_reaction=[],
        new_reaction=[ReactionTypeEmoji(emoji="👍")],
    )
    update = Update(update_id=1, message_reaction=reaction_update)

    await dp.feed_update(bot, update)

    bot.set_message_reaction.assert_called_once()
    kwargs = bot.set_message_reaction.call_args[1]
    assert kwargs["chat_id"] == -100123
    assert kwargs["message_id"] == 300


@pytest.mark.asyncio
async def test_router_copies_operator_media_that_carries_a_caption(mock_db):
    """A photo with a caption is media, not a text message.

    Reading the caption as the operator's reply delivered the words and dropped
    the screenshot — which is usually the whole point of sending it.
    """
    mock_db.topic_mappings_by_topic[42] = TopicMapping(
        user_id=100, topic_id=42, user_name="testuser"
    )

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.copy_message = AsyncMock(return_value=MagicMock(message_id=901))

    router = setup_router(
        sender=TelegramMessageSender(bot),
        llm_client=MagicMock(),
        forwarder=MagicMock(),
        db_manager=mock_db,
        chat_history_service=ChatHistoryService(),
        knowledge_gap_service=MagicMock(),
        command_handler=MagicMock(),
        photo_downloader=MagicMock(),
        message_buffer=MagicMock(),
        pipeline=MagicMock(),
        conversation_state=ConversationState(),
        support_group_chat_id=-100123,
    )

    dp = Dispatcher()
    dp.include_router(router)

    op_msg = Message(
        message_id=600,
        date=123456,
        chat=Chat(id=-100123, type="supergroup"),
        from_user=User(id=777, is_bot=False, first_name="Operator"),
        message_thread_id=42,
        photo=[PhotoSize(file_id="f1", file_unique_id="u1", width=100, height=100)],
        caption="Вот скриншот настроек",
    )

    await dp.feed_update(bot, Update(update_id=9, message=op_msg))

    bot.copy_message.assert_awaited_once()
    assert bot.copy_message.await_args.kwargs["chat_id"] == 100
    assert bot.copy_message.await_args.kwargs["message_id"] == 600

    # The only text sent is the in-topic delivery confirmation.
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert texts == ["Отправлено пользователю."]
