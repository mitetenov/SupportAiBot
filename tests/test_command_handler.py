"""Unit tests for SupportCommandHandler (/start, /help, /operator, /stats, /gaps, format_number)."""

from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.command_handler import SupportCommandHandler
from app.rag.knowledge_gaps import GapStatsDto
from app.storage.models import LlmTokenUsage, User


class MockDatabaseSessionManager:
    def __init__(self):
        self.users: dict[int, User] = {}
        self.token_usages: list[LlmTokenUsage] = []

    @asynccontextmanager
    async def session(self):
        session_mock = MagicMock()

        def add_mock(obj):
            if isinstance(obj, User):
                self.users[obj.telegram_id] = obj
            elif isinstance(obj, LlmTokenUsage):
                self.token_usages.append(obj)

        async def get_mock(model, key):
            if model == User:
                return self.users.get(key)
            return None

        async def execute_mock(stmt, params=None):
            result_mock = MagicMock()
            stmt_str = str(stmt)
            if "llm_token_usage" in stmt_str:
                grouped = defaultdict(lambda: {"total": 0, "prompt": 0, "completion": 0, "count": 0})
                for u in self.token_usages:
                    g = grouped[u.telegram_id]
                    g["total"] += u.total_tokens or 0
                    g["prompt"] += u.prompt_tokens or 0
                    g["completion"] += u.completion_tokens or 0
                    g["count"] += 1

                rows = []
                for tid, g in sorted(grouped.items(), key=lambda x: x[1]["total"], reverse=True):
                    row = MagicMock()
                    row.telegram_id = tid
                    row.total_tokens = g["total"]
                    row.prompt_tokens = g["prompt"]
                    row.completion_tokens = g["completion"]
                    row.request_count = g["count"]
                    rows.append(row)

                if "WHERE" in stmt_str or "where" in stmt_str:
                    # filter by telegram_id if present
                    for tid, g in grouped.items():
                        if str(tid) in stmt_str or any(str(tid) in str(p) for p in (params or {}).values()):
                            matched_row = MagicMock()
                            matched_row.telegram_id = tid
                            matched_row.total_tokens = g["total"]
                            matched_row.prompt_tokens = g["prompt"]
                            matched_row.completion_tokens = g["completion"]
                            matched_row.request_count = g["count"]
                            result_mock.fetchone.return_value = matched_row
                            return result_mock
                    # fallback to first if matched
                    result_mock.fetchone.return_value = rows[0] if rows else None
                else:
                    result_mock.fetchall.return_value = rows
                    result_mock.fetchone.return_value = rows[0] if rows else None
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (1000, "1.0K"),
        (1500, "1.5K"),
        (999999, "1000.0K"),
        (1000000, "1.0M"),
        (2500000, "2.5M"),
        (1000000000, "1.0B"),
    ],
)
def test_format_number(value: int, expected: str):
    assert SupportCommandHandler.format_number(value) == expected


def test_is_command():
    bot = MagicMock()
    handler = SupportCommandHandler(bot, MagicMock(), MagicMock(), admin_telegram_ids={111})

    assert handler.is_command("/start")
    assert handler.is_command("/stats 10")
    assert not handler.is_command("не работает")
    assert not handler.is_command(None)
    assert not handler.is_command("")


def test_is_admin():
    bot = MagicMock()
    handler = SupportCommandHandler(bot, MagicMock(), MagicMock(), admin_telegram_ids={111, 222})

    assert handler.is_admin(111)
    assert handler.is_admin(222)
    assert not handler.is_admin(333)


@pytest.mark.asyncio
async def test_non_admin_cannot_execute_admin_command(mock_db):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    gap_service = MagicMock()

    handler = SupportCommandHandler(
        bot=bot,
        db_manager=mock_db,
        knowledge_gap_service=gap_service,
        admin_telegram_ids={111},
    )

    handled = await handler.handle_admin_command(chat_id=900, telegram_id=999, text="/stats")
    assert not handled
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_admin_stats_top_users(mock_db):
    mock_db.users[100] = User(telegram_id=100, username="user100")
    mock_db.token_usages.append(
        LlmTokenUsage(
            telegram_id=100,
            prompt_tokens=500,
            completion_tokens=200,
            total_tokens=700,
        )
    )
    mock_db.token_usages.append(
        LlmTokenUsage(
            telegram_id=200,
            prompt_tokens=1000,
            completion_tokens=500,
            total_tokens=1500,
        )
    )

    bot = MagicMock()
    bot.send_message = AsyncMock()
    gap_service = MagicMock()

    handler = SupportCommandHandler(
        bot=bot,
        db_manager=mock_db,
        knowledge_gap_service=gap_service,
        admin_telegram_ids={111},
    )

    handled = await handler.handle_admin_command(chat_id=900, telegram_id=111, text="/stats 10")
    assert handled
    bot.send_message.assert_called_once()
    text = bot.send_message.call_args[1]["text"]
    assert "Топ-10 пользователей" in text
    assert "1.5K токенов" in text
    assert "@user100" in text


@pytest.mark.asyncio
async def test_admin_stats_single_user(mock_db):
    mock_db.users[123456] = User(telegram_id=123456, username="bob")
    mock_db.token_usages.append(
        LlmTokenUsage(
            telegram_id=123456,
            prompt_tokens=1000,
            completion_tokens=500,
            total_tokens=1500,
        )
    )

    bot = MagicMock()
    bot.send_message = AsyncMock()
    gap_service = MagicMock()

    handler = SupportCommandHandler(
        bot=bot,
        db_manager=mock_db,
        knowledge_gap_service=gap_service,
        admin_telegram_ids={111},
    )

    handled = await handler.handle_admin_command(chat_id=900, telegram_id=111, text="/stats 123456")
    assert handled
    bot.send_message.assert_called_once()
    text = bot.send_message.call_args[1]["text"]
    assert "Статистика @bob (123456):" in text
    assert "Запросов: 1" in text
    assert "Всего токенов: 1.5K" in text


@pytest.mark.asyncio
async def test_admin_gaps(mock_db):
    bot = MagicMock()
    bot.send_message = AsyncMock()

    now = datetime.now(UTC)
    gap_service = MagicMock()
    gap_service.get_top_gaps = AsyncMock(
        return_value=[
            GapStatsDto("как вернуть деньги", 7, "ESCALATED", now, now),
            GapStatsDto("не приходит смс", 3, "NO_MATCH", now, now),
        ]
    )

    handler = SupportCommandHandler(
        bot=bot,
        db_manager=mock_db,
        knowledge_gap_service=gap_service,
        admin_telegram_ids={111},
    )

    handled = await handler.handle_admin_command(chat_id=900, telegram_id=111, text="/gaps")
    assert handled
    bot.send_message.assert_called_once()
    text = bot.send_message.call_args[1]["text"]
    assert "Топ пробелов в знаниях:" in text
    assert "1. [7 раз] как вернуть деньги" in text
    assert "2. [3 раз] не приходит смс" in text
