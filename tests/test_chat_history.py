"""Unit tests for ChatHistoryService."""

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.chat_history import ChatHistoryService
from app.storage.database import DatabaseSessionManager
from app.storage.models import ChatMessage


class TestChatHistoryService:
    """Test ChatHistoryService in-memory LRU cache and database persistence."""

    @pytest.fixture
    def mock_db_manager(self) -> MagicMock:
        db_manager = MagicMock(spec=DatabaseSessionManager)
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.add = MagicMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result
        db_manager.session.return_value.__aenter__.return_value = mock_session
        return db_manager

    @pytest.mark.asyncio
    async def test_return_empty_history_for_unknown_user(self, mock_db_manager: MagicMock) -> None:
        mock_session = mock_db_manager.session.return_value.__aenter__.return_value
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        history = await service.get_history(1)
        assert history == []

    @pytest.mark.asyncio
    async def test_add_user_message(self, mock_db_manager: MagicMock) -> None:
        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        await service.add_user_message(1, "Hello")

        history = await service.get_history(1)
        assert len(history) == 1
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Hello"

    @pytest.mark.asyncio
    async def test_add_assistant_message(self, mock_db_manager: MagicMock) -> None:
        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        await service.add_assistant_message(1, "Hi there!")

        history = await service.get_history(1)
        assert len(history) == 1
        assert history[0]["role"] == "assistant"
        assert history[0]["content"] == "Hi there!"

    @pytest.mark.asyncio
    async def test_maintain_chronological_order(self, mock_db_manager: MagicMock) -> None:
        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        await service.add_user_message(1, "Q1")
        await service.add_assistant_message(1, "A1")
        await service.add_user_message(1, "Q2")
        await service.add_assistant_message(1, "A2")

        history = await service.get_history(1)
        assert len(history) == 4
        assert history[0] == {"role": "user", "content": "Q1"}
        assert history[1] == {"role": "assistant", "content": "A1"}
        assert history[2] == {"role": "user", "content": "Q2"}
        assert history[3] == {"role": "assistant", "content": "A2"}

    @pytest.mark.asyncio
    async def test_rotate_old_messages_beyond_max(self, mock_db_manager: MagicMock) -> None:
        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        for i in range(30):
            await service.add_user_message(1, f"msg{i}")
            await service.add_assistant_message(1, f"rsp{i}")

        history = await service.get_history(1)
        assert len(history) == 20
        assert history[0]["content"] == "msg20"
        assert history[-1]["content"] == "rsp29"

    @pytest.mark.asyncio
    async def test_clear_history(self, mock_db_manager: MagicMock) -> None:
        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        await service.add_user_message(1, "Hello")
        await service.add_assistant_message(1, "Hi")

        await service.clear(1)
        mock_session = mock_db_manager.session.return_value.__aenter__.return_value
        mock_session.execute.assert_awaited()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        history = await service.get_history(1)
        assert history == []

    @pytest.mark.asyncio
    async def test_keep_users_separate(self, mock_db_manager: MagicMock) -> None:
        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        await service.add_user_message(1, "U1")
        await service.add_user_message(2, "U2")

        h1 = await service.get_history(1)
        h2 = await service.get_history(2)

        assert len(h1) == 1
        assert len(h2) == 1
        assert h1[0]["content"] == "U1"
        assert h2[0]["content"] == "U2"

    @pytest.mark.asyncio
    async def test_clear_should_only_affect_specified_user(
        self, mock_db_manager: MagicMock
    ) -> None:
        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        await service.add_user_message(1, "U1")
        await service.add_user_message(2, "U2")

        await service.clear(1)

        mock_session = mock_db_manager.session.return_value.__aenter__.return_value
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        assert (await service.get_history(1)) == []
        assert len(await service.get_history(2)) == 1

    @pytest.mark.asyncio
    async def test_to_gemini_contents_conversion(self, mock_db_manager: MagicMock) -> None:
        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        await service.add_user_message(1, "Hello")
        await service.add_assistant_message(1, "Hi")

        contents = await service.to_gemini_contents(1)
        assert len(contents) == 2
        assert contents[0] == {"role": "user", "parts": [{"text": "Hello"}]}
        assert contents[1] == {"role": "model", "parts": [{"text": "Hi"}]}

    @pytest.mark.asyncio
    async def test_to_gemini_contents_empty_for_unknown_user(
        self, mock_db_manager: MagicMock
    ) -> None:
        mock_session = mock_db_manager.session.return_value.__aenter__.return_value
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        contents = await service.to_gemini_contents(999)
        assert contents == []

    @pytest.mark.asyncio
    async def test_return_defensive_copy(self, mock_db_manager: MagicMock) -> None:
        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        await service.add_user_message(1, "Hello")

        history = await service.get_history(1)
        history.append({"role": "user", "content": "injected"})

        fresh_history = await service.get_history(1)
        assert len(fresh_history) == 1

    @pytest.mark.asyncio
    async def test_ignore_none_or_blank_messages(self, mock_db_manager: MagicMock) -> None:
        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        await service.add_user_message(1, None)
        await service.add_assistant_message(1, None)
        await service.add_user_message(1, "")
        await service.add_assistant_message(1, "   ")

        assert len(await service.get_history(1)) == 0

    @pytest.mark.asyncio
    async def test_load_from_database_reverses_newest_first_order(
        self, mock_db_manager: MagicMock
    ) -> None:
        now = datetime.now(UTC)
        # Database returns newest-first: newest, middle, oldest
        db_messages = [
            ChatMessage(telegram_id=7, role="assistant", content="newest", created_at=now),
            ChatMessage(telegram_id=7, role="user", content="middle", created_at=now),
            ChatMessage(telegram_id=7, role="assistant", content="oldest", created_at=now),
        ]

        mock_session = mock_db_manager.session.return_value.__aenter__.return_value
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = db_messages
        mock_session.execute.return_value = mock_result

        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        history = await service.get_history(7)

        assert len(history) == 3
        assert history[0]["content"] == "oldest"
        assert history[1]["content"] == "middle"
        assert history[2]["content"] == "newest"

    @pytest.mark.asyncio
    async def test_get_last_user_message(self, mock_db_manager: MagicMock) -> None:
        now = datetime.now(UTC)
        db_messages = [
            ChatMessage(telegram_id=7, role="user", content="latest question", created_at=now),
            ChatMessage(telegram_id=7, role="assistant", content="an older answer", created_at=now),
            ChatMessage(telegram_id=7, role="user", content="an older question", created_at=now),
        ]

        mock_session = mock_db_manager.session.return_value.__aenter__.return_value
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = db_messages
        mock_session.execute.return_value = mock_result

        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        await service.get_history(7)

        assert service.get_last_user_message(7) == "latest question"
        assert service.get_last_user_message(999) is None

    @pytest.mark.asyncio
    async def test_rejected_faq_questions_management(self) -> None:
        service = ChatHistoryService(db_manager=None, max_messages=20, ttl_days=7)
        assert service.get_rejected_faq_questions(1) == set()

        service.add_rejected_faq_questions(1, {"Q1", "Q2"})
        assert service.get_rejected_faq_questions(1) == {"Q1", "Q2"}

        service.add_rejected_faq_questions(1, {"Q3"})
        assert service.get_rejected_faq_questions(1) == {"Q1", "Q2", "Q3"}

        # If user message is a rejection, questions are retained
        service.clear_rejected_faqs_if_new_topic(1, "это не то")
        assert service.get_rejected_faq_questions(1) == {"Q1", "Q2", "Q3"}

        service.clear_rejected_faqs_if_new_topic(1, "не подходит совсем")
        assert service.get_rejected_faq_questions(1) == {"Q1", "Q2", "Q3"}

        # If user message is a new topic, questions are cleared
        service.clear_rejected_faqs_if_new_topic(1, "Как настроить VPN на телефоне?")
        assert service.get_rejected_faq_questions(1) == set()

    @pytest.mark.asyncio
    async def test_evict_stale_entries(self, mock_db_manager: MagicMock) -> None:
        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        await service.add_user_message(1, "active user")
        await service.add_user_message(2, "stale user")

        # Fake last activity for user 2 to 10 days ago
        service._last_activity[2] = time.time() - (10 * 86400)

        await service.evict_stale_entries(ttl_days=7)

        # User 2 in-memory evicted, User 1 retained
        assert 1 in service._histories
        assert 2 not in service._histories

        # DB cleanup executed
        mock_session = mock_db_manager.session.return_value.__aenter__.return_value
        mock_session.execute.assert_awaited()

    @pytest.mark.asyncio
    async def test_database_persistence_error_handling(self, mock_db_manager: MagicMock) -> None:
        mock_session = mock_db_manager.session.return_value.__aenter__.return_value
        mock_session.commit.side_effect = RuntimeError("DB write error")

        service = ChatHistoryService(db_manager=mock_db_manager, max_messages=20, ttl_days=7)
        # Should not raise exception
        await service.add_user_message(1, "Hello")
        await service.add_assistant_message(1, "Hi")

        history = await service.get_history(1)
        assert len(history) == 2
