"""Unit tests for SupportGroupForwarder (forwarding messages, error reporting, message mapping)."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.forwarder import SupportGroupForwarder
from app.bot.sender import TelegramMessageSender
from app.storage.models import MessageMapping


class DummyUser:
    def __init__(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ):
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name


class DummyCopyMessageResult:
    def __init__(self, message_id: int):
        self.message_id = message_id


class MockDatabaseSessionManager:
    def __init__(self):
        self.message_mappings: list[MessageMapping] = []

    @asynccontextmanager
    async def session(self):
        session_mock = MagicMock()

        def add_mock(obj):
            if isinstance(obj, MessageMapping):
                self.message_mappings.append(obj)

        session_mock.add = add_mock
        session_mock.commit = AsyncMock()
        session_mock.rollback = AsyncMock()
        session_mock.close = AsyncMock()
        yield session_mock


@pytest.fixture
def mock_db():
    return MockDatabaseSessionManager()


@pytest.mark.asyncio
async def test_forward_to_support_with_existing_topic(mock_db):
    bot = MagicMock()
    bot.copy_message = AsyncMock(return_value=DummyCopyMessageResult(200))
    bot.send_message = AsyncMock()

    topic_manager = MagicMock()
    topic_manager.resolve_topic_id = AsyncMock(return_value=42)

    forwarder = SupportGroupForwarder(
        sender=TelegramMessageSender(bot),
        topic_manager=topic_manager,
        db_manager=mock_db,
        support_group_chat_id=-100123,
        admin_username="admin",
    )

    user = DummyUser(1, username="johndoe")
    await forwarder.forward_to_support(
        user_chat_id=1,
        user_message_ids=[100],
        user=user,
        bot_response="Bot response",
        needs_escalation=False,
    )

    topic_manager.resolve_topic_id.assert_called_once_with(1, "@johndoe")
    bot.copy_message.assert_called_once_with(
        chat_id=-100123,
        from_chat_id=1,
        message_id=100,
        message_thread_id=42,
    )
    bot.send_message.assert_called_once()
    args, kwargs = bot.send_message.call_args
    assert kwargs["chat_id"] == -100123
    assert kwargs["message_thread_id"] == 42
    assert "Bot response" in kwargs["text"]
    assert "@admin" not in kwargs["text"]

    assert len(mock_db.message_mappings) == 1
    assert mock_db.message_mappings[0].topic_message_id == 200
    assert mock_db.message_mappings[0].topic_id == 42
    assert mock_db.message_mappings[0].user_chat_id == 1
    assert mock_db.message_mappings[0].user_message_id == 100


@pytest.mark.asyncio
async def test_forward_to_support_with_escalation_includes_admin_tag(mock_db):
    bot = MagicMock()
    bot.copy_message = AsyncMock(return_value=DummyCopyMessageResult(200))
    bot.send_message = AsyncMock()

    topic_manager = MagicMock()
    topic_manager.resolve_topic_id = AsyncMock(return_value=42)

    forwarder = SupportGroupForwarder(
        sender=TelegramMessageSender(bot),
        topic_manager=topic_manager,
        db_manager=mock_db,
        support_group_chat_id=-100123,
        admin_username="superadmin",
    )

    user = DummyUser(1, username="johndoe")
    await forwarder.forward_to_support(
        user_chat_id=1,
        user_message_ids=[100],
        user=user,
        bot_response="Escalated response",
        needs_escalation=True,
    )

    bot.send_message.assert_called_once()
    kwargs = bot.send_message.call_args[1]
    assert "@superadmin" in kwargs["text"]


@pytest.mark.asyncio
async def test_recreate_topic_when_first_copy_fails(mock_db):
    bot = MagicMock()
    bot.copy_message = AsyncMock(
        side_effect=[
            Exception("Topic closed / not found"),
            DummyCopyMessageResult(201),
        ]
    )
    bot.send_message = AsyncMock()

    topic_manager = MagicMock()
    topic_manager.resolve_topic_id = AsyncMock(return_value=42)
    topic_manager.recreate_stale_topic = AsyncMock(return_value=99)

    forwarder = SupportGroupForwarder(
        sender=TelegramMessageSender(bot),
        topic_manager=topic_manager,
        db_manager=mock_db,
        support_group_chat_id=-100123,
    )

    user = DummyUser(1, username="johndoe")
    await forwarder.forward_to_support(
        user_chat_id=1,
        user_message_ids=[100],
        user=user,
        bot_response="Answer",
        needs_escalation=False,
    )

    topic_manager.recreate_stale_topic.assert_called_once_with(1, "@johndoe", 42)
    assert bot.copy_message.call_count == 2
    bot.send_message.assert_called_once()
    assert bot.send_message.call_args[1]["message_thread_id"] == 99


@pytest.mark.asyncio
async def test_resolve_user_name_variants(mock_db):
    forwarder = SupportGroupForwarder(
        sender=TelegramMessageSender(MagicMock()),
        topic_manager=MagicMock(),
        db_manager=mock_db,
        support_group_chat_id=-100123,
    )

    assert forwarder.resolve_user_name(DummyUser(1, username="test")) == "@test"
    assert (
        forwarder.resolve_user_name(DummyUser(2, first_name="John", last_name="Doe")) == "John Doe"
    )
    assert forwarder.resolve_user_name(DummyUser(3, first_name="John")) == "John"
    assert forwarder.resolve_user_name(DummyUser(4)) == "User 4"


@pytest.mark.asyncio
async def test_forward_error_to_topic(mock_db):
    bot = MagicMock()
    bot.send_message = AsyncMock()

    topic_manager = MagicMock()
    topic_manager.resolve_topic_id = AsyncMock(return_value=42)

    forwarder = SupportGroupForwarder(
        sender=TelegramMessageSender(bot),
        topic_manager=topic_manager,
        db_manager=mock_db,
        support_group_chat_id=-100123,
        admin_username="admin",
    )

    user = DummyUser(1, username="johndoe")
    await forwarder.forward_error_to_topic(
        user=user,
        user_message="My message",
        user_visible_message="Error response",
        error_details="DeepSeek 500 server error",
    )

    assert bot.send_message.call_count == 2
    first_call_text = bot.send_message.call_args_list[0][1]["text"]
    second_call_text = bot.send_message.call_args_list[1][1]["text"]

    assert "My message" in first_call_text
    assert "Error response" in first_call_text
    assert "@admin" in second_call_text
    assert "DeepSeek 500 server error" in second_call_text


@pytest.mark.asyncio
async def test_the_illustration_lands_in_the_topic_after_the_answer(mock_db):
    """The operator needs the picture in the thread to point at it in a reply.

    Copying it out of the user's own chat is also what records the mapping, so
    an operator replying to it reaches the user as a reply to that same picture.
    """
    bot = MagicMock()
    bot.copy_message = AsyncMock(
        side_effect=[DummyCopyMessageResult(200), DummyCopyMessageResult(201)]
    )
    bot.send_message = AsyncMock()

    topic_manager = MagicMock()
    topic_manager.resolve_topic_id = AsyncMock(return_value=42)

    forwarder = SupportGroupForwarder(
        sender=TelegramMessageSender(bot),
        topic_manager=topic_manager,
        db_manager=mock_db,
        support_group_chat_id=-100123,
        admin_username="admin",
    )

    await forwarder.forward_to_support(
        user_chat_id=1,
        user_message_ids=[100],
        user=DummyUser(1, username="johndoe"),
        bot_response="Нажмите левую кнопку",
        needs_escalation=False,
        illustration_message_id=907,
    )

    assert bot.copy_message.await_args_list[-1].kwargs["message_id"] == 907
    # Text first, picture after: the thread reads in the order the user saw it.
    assert bot.send_message.await_count == 1

    picture_mapping = mock_db.message_mappings[-1]
    assert picture_mapping.topic_message_id == 201
    assert picture_mapping.user_chat_id == 1
    assert picture_mapping.user_message_id == 907


@pytest.mark.asyncio
async def test_no_illustration_means_nothing_extra_is_copied(mock_db):
    bot = MagicMock()
    bot.copy_message = AsyncMock(return_value=DummyCopyMessageResult(200))
    bot.send_message = AsyncMock()

    topic_manager = MagicMock()
    topic_manager.resolve_topic_id = AsyncMock(return_value=42)

    forwarder = SupportGroupForwarder(
        sender=TelegramMessageSender(bot),
        topic_manager=topic_manager,
        db_manager=mock_db,
        support_group_chat_id=-100123,
    )

    await forwarder.forward_to_support(
        user_chat_id=1,
        user_message_ids=[100],
        user=DummyUser(1),
        bot_response="Ответ",
        needs_escalation=False,
    )

    assert bot.copy_message.await_count == 1
