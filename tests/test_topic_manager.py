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
