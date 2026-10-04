"""Unit tests for TopicManager (topic resolution, creation, concurrency, stale topic recreation)."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.topic_manager import TopicManager
from app.storage.models import TopicMapping


class DummyForumTopic:
    def __init__(self, message_thread_id: int):
        self.message_thread_id = message_thread_id


class MockDatabaseSessionManager:
    def __init__(self):
        self.topic_mappings = {}

    @asynccontextmanager
    async def session(self):
        session_mock = MagicMock()

        def add_mock(obj):
            if isinstance(obj, TopicMapping):
                self.topic_mappings[obj.user_id] = obj

        async def execute_mock(stmt, params=None):
            result_mock = MagicMock()
            stmt_str = str(stmt)
            if "DELETE" in stmt_str:
                self.topic_mappings.clear()
            else:
                mappings = list(self.topic_mappings.values())
                result_mock.scalar_one_or_none.return_value = mappings[0] if mappings else None
            return result_mock

        session_mock.add = add_mock
        session_mock.execute = AsyncMock(side_effect=execute_mock)
        session_mock.commit = AsyncMock()
        session_mock.rollback = AsyncMock()
        session_mock.close = AsyncMock()

        yield session_mock


@pytest.fixture
def mock_db():
    return MockDatabaseSessionManager()


@pytest.mark.asyncio
async def test_resolve_topic_id_returns_existing(mock_db):
    mock_db.topic_mappings[1] = TopicMapping(user_id=1, topic_id=42, user_name="user1")

    bot = MagicMock()
    bot.create_forum_topic = AsyncMock()

    manager = TopicManager(mock_db, bot, support_group_chat_id=-100123)
    topic_id = await manager.resolve_topic_id(1, "user1")

    assert topic_id == 42
    bot.create_forum_topic.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_topic_id_creates_when_not_found(mock_db):
    bot = MagicMock()
    bot.create_forum_topic = AsyncMock(return_value=DummyForumTopic(55))

    manager = TopicManager(mock_db, bot, support_group_chat_id=-100123)
    topic_id = await manager.resolve_topic_id(1, "testuser")

    assert topic_id == 55
    bot.create_forum_topic.assert_called_once_with(
        chat_id=-100123,
        name="testuser (ID: 1)",
    )
    assert 1 in mock_db.topic_mappings
    assert mock_db.topic_mappings[1].topic_id == 55


@pytest.mark.asyncio
async def test_resolve_topic_id_handles_creation_failure(mock_db):
    bot = MagicMock()
    bot.create_forum_topic = AsyncMock(side_effect=Exception("Chat not found"))

    manager = TopicManager(mock_db, bot, support_group_chat_id=-100123)
    topic_id = await manager.resolve_topic_id(1, "testuser")

    assert topic_id is None


@pytest.mark.asyncio
async def test_build_topic_name_variants(mock_db):
    bot = MagicMock()
    manager = TopicManager(mock_db, bot, support_group_chat_id=-100123)

    assert manager._build_topic_name(1, "johndoe") == "johndoe (ID: 1)"
    assert manager._build_topic_name(2, None) == "User 2"
    assert manager._build_topic_name(3, "   ") == "User 3"
    assert manager._build_topic_name(4, "") == "User 4"
    assert manager._build_topic_name(-55, "Jane Doe") == "Jane Doe (ID: 55)"
    assert manager._build_topic_name(-55, None) == "User 55"
    assert (
        manager._build_topic_name(-55, "Jane Doe", "jane@example.com")
        == "Jane Doe (ID: jane@example.com)"
    )
    assert (
        manager._build_topic_name(-55, "@jane", "jane@example.com")
        == "@jane (ID: jane@example.com)"
    )
    assert manager._build_topic_name(55, "@jane", "jane@example.com") == "@jane (ID: 55)"
    long_name = manager._build_topic_name(-55, "J" * 200, "jane@example.com")
    assert len(long_name) == 128
    assert long_name.endswith(" (ID: jane@example.com)")


@pytest.mark.asyncio
async def test_renames_an_existing_cabinet_topic_without_changing_its_mapping(mock_db):
    mapping = TopicMapping(user_id=-55, topic_id=42, user_name="Кабинет #55", active_ticket_id=17)
    mock_db.topic_mappings[-55] = mapping
    bot = MagicMock()
    bot.edit_forum_topic = AsyncMock(return_value=True)
    bot.create_forum_topic = AsyncMock()
    manager = TopicManager(mock_db, bot, support_group_chat_id=-100123)

    assert await manager.resolve_topic_id(-55, "Jane Doe", display_id="jane@example.com") == 42
    assert await manager.resolve_topic_id(-55, "Jane Doe", display_id="jane@example.com") == 42

    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=-100123, message_thread_id=42, name="Jane Doe (ID: jane@example.com)"
    )
    bot.create_forum_topic.assert_not_awaited()
    assert mapping.user_name == "Jane Doe (ID: jane@example.com)"
    assert mapping.user_id == -55
    assert mapping.active_ticket_id == 17


@pytest.mark.asyncio
async def test_a_failed_rename_keeps_the_topic_and_retries_on_the_next_turn(mock_db):
    mapping = TopicMapping(user_id=-55, topic_id=42, user_name="Кабинет #55")
    mock_db.topic_mappings[-55] = mapping
    bot = MagicMock()
    bot.edit_forum_topic = AsyncMock(side_effect=[RuntimeError("Telegram unavailable"), True])
    manager = TopicManager(mock_db, bot, support_group_chat_id=-100123)

    assert await manager.resolve_topic_id(-55, "Jane Doe") == 42
    assert mapping.user_name == "Кабинет #55"
    assert await manager.resolve_topic_id(-55, "Jane Doe") == 42
    assert mapping.user_name == "Jane Doe (ID: 55)"
    assert bot.edit_forum_topic.await_count == 2


@pytest.mark.asyncio
async def test_a_ticket_profile_does_not_rename_an_existing_telegram_topic(mock_db):
    mapping = TopicMapping(user_id=55, topic_id=42, user_name="@jane")
    mock_db.topic_mappings[55] = mapping
    bot = MagicMock()
    bot.edit_forum_topic = AsyncMock()
    manager = TopicManager(mock_db, bot, support_group_chat_id=-100123)

    assert await manager.resolve_topic_id(55, "Jane Doe") == 42
    bot.edit_forum_topic.assert_not_awaited()
    assert mapping.user_name == "@jane"


@pytest.mark.asyncio
async def test_an_email_change_renames_the_same_topic_even_when_the_name_is_unchanged(mock_db):
    mapping = TopicMapping(
        user_id=-55, topic_id=42, user_name="Jane Doe (ID: old@example.com)", active_ticket_id=17
    )
    mock_db.topic_mappings[-55] = mapping
    bot = MagicMock()
    bot.edit_forum_topic = AsyncMock(return_value=True)
    manager = TopicManager(mock_db, bot, support_group_chat_id=-100123)

    assert await manager.resolve_topic_id(-55, "Jane Doe", display_id="new@example.com") == 42
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=-100123, message_thread_id=42, name="Jane Doe (ID: new@example.com)"
    )
    assert mapping.user_id == -55
    assert mapping.active_ticket_id == 17
    assert mapping.user_name == "Jane Doe (ID: new@example.com)"


@pytest.mark.asyncio
async def test_creates_a_cabinet_topic_with_email_without_changing_the_storage_key(mock_db):
    bot = MagicMock()
    bot.create_forum_topic = AsyncMock(return_value=DummyForumTopic(42))
    bot.edit_forum_topic = AsyncMock()
    manager = TopicManager(mock_db, bot, support_group_chat_id=-100123)

    assert await manager.resolve_topic_id(-55, "@jane", display_id="jane@example.com") == 42
    assert await manager.resolve_topic_id(-55, "@jane", display_id="jane@example.com") == 42
    bot.create_forum_topic.assert_awaited_once_with(
        chat_id=-100123, name="@jane (ID: jane@example.com)"
    )
    bot.edit_forum_topic.assert_not_awaited()
    assert mock_db.topic_mappings[-55].user_name == "@jane (ID: jane@example.com)"
    assert 55 not in mock_db.topic_mappings


@pytest.mark.asyncio
async def test_recreate_stale_topic(mock_db):
    mock_db.topic_mappings[1] = TopicMapping(user_id=1, topic_id=42, user_name="user1")

    bot = MagicMock()
    bot.create_forum_topic = AsyncMock(return_value=DummyForumTopic(99))

    manager = TopicManager(mock_db, bot, support_group_chat_id=-100123)
    new_topic_id = await manager.recreate_stale_topic(1, "user1", 42)

    assert new_topic_id == 99
    assert mock_db.topic_mappings[1].topic_id == 99


@pytest.mark.asyncio
async def test_recreate_stale_topic_not_deleting_if_topic_id_different(mock_db):
    mock_db.topic_mappings[1] = TopicMapping(user_id=1, topic_id=100, user_name="user1")

    bot = MagicMock()
    bot.create_forum_topic = AsyncMock(return_value=DummyForumTopic(200))

    manager = TopicManager(mock_db, bot, support_group_chat_id=-100123)
    new_topic_id = await manager.recreate_stale_topic(1, "user1", 42)

    assert new_topic_id == 200


@pytest.mark.asyncio
async def test_concurrent_resolve_topic_id(mock_db):
    call_count = 0

    async def mock_create(chat_id, name):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.02)
        return DummyForumTopic(777)

    bot = MagicMock()
    bot.create_forum_topic = AsyncMock(side_effect=mock_create)

    manager = TopicManager(mock_db, bot, support_group_chat_id=-100123)

    tasks = [manager.resolve_topic_id(1, "user1") for _ in range(5)]
    results = await asyncio.gather(*tasks)

    assert all(r == 777 for r in results)
    assert call_count == 1
