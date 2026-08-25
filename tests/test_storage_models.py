"""Unit tests for SQLAlchemy storage models and DatabaseSessionManager."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import BigInteger, DateTime, Float, Integer, String, Text, inspect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.storage.database import DatabaseSessionManager
from app.storage.models import (
    ChatMessage,
    Faq,
    FaqMetadata,
    KnowledgeGap,
    LlmTokenUsage,
    MessageMapping,
    TopicMapping,
    User,
)


class TestStorageModelsSchema:
    """Validate table definitions, columns, constraints, and indexes."""

    def test_user_model_schema(self) -> None:
        assert User.__tablename__ == "user_names"
        mapper = inspect(User)
        columns = {c.name: c for c in mapper.columns}

        assert "telegram_id" in columns
        assert isinstance(columns["telegram_id"].type, BigInteger)
        assert columns["telegram_id"].primary_key is True

        assert "username" in columns
        assert isinstance(columns["username"].type, String)
        assert columns["username"].nullable is True

        assert "first_name" in columns
        assert isinstance(columns["first_name"].type, String)
        assert columns["first_name"].nullable is True

        assert "last_name" in columns
        assert isinstance(columns["last_name"].type, String)
        assert columns["last_name"].nullable is True

        assert "updated_at" in columns
        assert isinstance(columns["updated_at"].type, DateTime)

    def test_user_model_instantiation(self) -> None:
        now = datetime.now(UTC)
        user = User(
            telegram_id=123456789,
            username="testuser",
            first_name="Test",
            last_name="User",
            updated_at=now,
        )
        assert user.telegram_id == 123456789
        assert user.username == "testuser"
        assert user.first_name == "Test"
        assert user.last_name == "User"
        assert user.updated_at == now

    def test_topic_mapping_model_schema(self) -> None:
        assert TopicMapping.__tablename__ == "topic_mappings"
        mapper = inspect(TopicMapping)
        columns = {c.name: c for c in mapper.columns}

        assert "user_id" in columns
        assert isinstance(columns["user_id"].type, BigInteger)
        assert columns["user_id"].primary_key is True

        assert "topic_id" in columns
        assert isinstance(columns["topic_id"].type, Integer)
        assert columns["topic_id"].nullable is False

        assert "user_name" in columns
        assert isinstance(columns["user_name"].type, String)
        assert columns["user_name"].nullable is True

        assert "active_ticket_id" in columns
        assert isinstance(columns["active_ticket_id"].type, BigInteger)
        assert columns["active_ticket_id"].nullable is True

        assert "created_at" in columns
        assert isinstance(columns["created_at"].type, DateTime)

    def test_topic_mapping_instantiation(self) -> None:
        now = datetime.now(UTC)
        mapping = TopicMapping(
            user_id=123456789,
            topic_id=42,
            user_name="John Doe",
            created_at=now,
        )
        assert mapping.user_id == 123456789
        assert mapping.topic_id == 42
        assert mapping.user_name == "John Doe"
        assert mapping.created_at == now

    def test_message_mapping_model_schema(self) -> None:
        assert MessageMapping.__tablename__ == "message_mappings"
        mapper = inspect(MessageMapping)
        columns = {c.name: c for c in mapper.columns}

        assert "id" in columns
        assert isinstance(columns["id"].type, BigInteger)
        assert columns["id"].primary_key is True

        assert "topic_message_id" in columns
        assert isinstance(columns["topic_message_id"].type, Integer)
        assert columns["topic_message_id"].nullable is False

        assert "topic_id" in columns
        assert isinstance(columns["topic_id"].type, Integer)
        assert columns["topic_id"].nullable is False

        assert "user_chat_id" in columns
        assert isinstance(columns["user_chat_id"].type, BigInteger)
        assert columns["user_chat_id"].nullable is False

        assert "user_message_id" in columns
        assert isinstance(columns["user_message_id"].type, Integer)
        assert columns["user_message_id"].nullable is False

        assert "created_at" in columns
        assert isinstance(columns["created_at"].type, DateTime)

    def test_message_mapping_instantiation(self) -> None:
        now = datetime.now(UTC)
        mapping = MessageMapping(
            topic_message_id=100,
            topic_id=200,
            user_chat_id=123456789,
            user_message_id=42,
            created_at=now,
        )
        assert mapping.topic_message_id == 100
        assert mapping.topic_id == 200
        assert mapping.user_chat_id == 123456789
        assert mapping.user_message_id == 42
        assert mapping.created_at == now

    def test_chat_message_model_schema(self) -> None:
        assert ChatMessage.__tablename__ == "chat_messages"
        mapper = inspect(ChatMessage)
        columns = {c.name: c for c in mapper.columns}

        assert "id" in columns
        assert isinstance(columns["id"].type, BigInteger)
        assert columns["id"].primary_key is True

        assert "telegram_id" in columns
        assert isinstance(columns["telegram_id"].type, BigInteger)
        assert columns["telegram_id"].nullable is False

        assert "role" in columns
        assert isinstance(columns["role"].type, String)
        assert columns["role"].nullable is False

        assert "content" in columns
        assert isinstance(columns["content"].type, (Text, String))
        assert columns["content"].nullable is False

        assert "created_at" in columns
        assert isinstance(columns["created_at"].type, DateTime)
        assert columns["created_at"].nullable is False

        # Verify composite index on (telegram_id, created_at)
        indexes = {idx.name: idx for idx in ChatMessage.__table__.indexes}
        assert "idx_chat_messages_telegram_id" in indexes
        idx = indexes["idx_chat_messages_telegram_id"]
        indexed_cols = [c.name for c in idx.columns]
        assert indexed_cols == ["telegram_id", "created_at"]

    def test_chat_message_instantiation(self) -> None:
        now = datetime.now(UTC)
        msg = ChatMessage(
            telegram_id=123456789,
            role="user",
            content="Hello support",
            created_at=now,
        )
        assert msg.telegram_id == 123456789
        assert msg.role == "user"
        assert msg.content == "Hello support"
        assert msg.created_at == now

    def test_llm_token_usage_model_schema(self) -> None:
        assert LlmTokenUsage.__tablename__ == "llm_token_usage"
        mapper = inspect(LlmTokenUsage)
        columns = {c.name: c for c in mapper.columns}

        assert "id" in columns
        assert isinstance(columns["id"].type, BigInteger)
        assert columns["id"].primary_key is True

        assert "telegram_id" in columns
        assert isinstance(columns["telegram_id"].type, BigInteger)
        assert columns["telegram_id"].nullable is True

        assert "prompt_tokens" in columns
        assert isinstance(columns["prompt_tokens"].type, BigInteger)
        assert "completion_tokens" in columns
        assert isinstance(columns["completion_tokens"].type, BigInteger)
        assert "total_tokens" in columns
        assert isinstance(columns["total_tokens"].type, BigInteger)
        assert "created_at" in columns
        assert isinstance(columns["created_at"].type, DateTime)

    def test_knowledge_gap_model_schema(self) -> None:
        assert KnowledgeGap.__tablename__ == "knowledge_gaps"
        mapper = inspect(KnowledgeGap)
        columns = {c.name: c for c in mapper.columns}

        assert "id" in columns
        assert isinstance(columns["id"].type, BigInteger)
        assert columns["id"].primary_key is True

        assert "user_query" in columns
        assert isinstance(columns["user_query"].type, String)
        assert columns["user_query"].nullable is False

        assert "embedding" in columns

        assert "best_faq_question" in columns
        assert "max_similarity" in columns
        assert isinstance(columns["max_similarity"].type, Float)
        assert "faq_count" in columns
        assert isinstance(columns["faq_count"].type, Integer)
        assert "trigger_reason" in columns
        assert "bot_response" in columns
        assert "gap_count" in columns
        assert isinstance(columns["gap_count"].type, Integer)
        assert "first_seen" in columns
        assert isinstance(columns["first_seen"].type, DateTime)
        assert "last_seen" in columns
        assert isinstance(columns["last_seen"].type, DateTime)
        assert "telegram_id" in columns
        assert isinstance(columns["telegram_id"].type, BigInteger)

    def test_faq_and_faq_metadata_models_schema(self) -> None:
        assert Faq.__tablename__ == "faq"
        faq_cols = {c.name: c for c in inspect(Faq).columns}
        assert "id" in faq_cols
        assert faq_cols["id"].primary_key is True
        assert "question" in faq_cols
        assert "answer" in faq_cols
        assert "embedding" in faq_cols
        assert "keywords" in faq_cols

        assert FaqMetadata.__tablename__ == "faq_metadata"
        meta_cols = {c.name: c for c in inspect(FaqMetadata).columns}
        assert "key" in meta_cols
        assert meta_cols["key"].primary_key is True
        assert "val" in meta_cols


class TestDatabaseSessionManager:
    """Test DatabaseSessionManager engine creation, session lifecycle, and disposal."""

    def test_init_sets_properties(self) -> None:
        manager = DatabaseSessionManager("postgresql+asyncpg://user:pass@localhost:5432/db")
        assert manager.is_initialized is True
        assert manager.engine is not None
        assert manager.sessionmaker is not None

    def test_lazy_init(self) -> None:
        manager = DatabaseSessionManager()
        assert manager.is_initialized is False
        manager.init("postgresql+asyncpg://user:pass@localhost:5432/db")
        assert manager.is_initialized is True

    @pytest.mark.asyncio
    async def test_session_context_manager_commit(self) -> None:
        manager = DatabaseSessionManager("postgresql+asyncpg://user:pass@localhost:5432/db")

        mock_session = AsyncMock(spec=AsyncSession)
        manager._sessionmaker = MagicMock(return_value=mock_session)

        async with manager.session() as session:
            assert session is mock_session

        mock_session.commit.assert_awaited_once()
        mock_session.rollback.assert_not_awaited()
        mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_session_context_manager_rollback_on_error(self) -> None:
        manager = DatabaseSessionManager("postgresql+asyncpg://user:pass@localhost:5432/db")

        mock_session = AsyncMock(spec=AsyncSession)
        manager._sessionmaker = MagicMock(return_value=mock_session)

        with pytest.raises(RuntimeError, match="DB operation failed"):
            async with manager.session():
                raise RuntimeError("DB operation failed")

        mock_session.commit.assert_not_awaited()
        mock_session.rollback.assert_awaited_once()
        mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_disposes_engine(self) -> None:
        manager = DatabaseSessionManager("postgresql+asyncpg://user:pass@localhost:5432/db")
        mock_engine = AsyncMock(spec=AsyncEngine)
        manager._engine = mock_engine

        await manager.close()
        mock_engine.dispose.assert_awaited_once()
        assert manager.is_initialized is False

    @pytest.mark.asyncio
    async def test_init_models_creates_tables(self) -> None:
        manager = DatabaseSessionManager("postgresql+asyncpg://user:pass@localhost:5432/db")
        mock_conn = AsyncMock()
        mock_engine = AsyncMock(spec=AsyncEngine)
        mock_engine.begin.return_value.__aenter__.return_value = mock_conn
        manager._engine = mock_engine

        await manager.init_models()
        mock_conn.run_sync.assert_awaited()
