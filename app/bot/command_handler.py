"""Handles bot slash commands (/start, /help, /operator, /stats, /gaps)."""

import logging
from collections.abc import Iterable

from aiogram import Bot
from sqlalchemy import func, select

from app.constants import get_message
from app.rag.knowledge_gaps import KnowledgeGapService
from app.storage.database import DatabaseSessionManager
from app.storage.models import LlmTokenUsage, User

logger = logging.getLogger(__name__)


class SupportCommandHandler:
    """Processes user slash commands and administrative stats/gap inspections."""

    STATS_ID_THRESHOLD: int = 100
    DEFAULT_STATS_LIMIT: int = 10

    def __init__(
        self,
        bot: Bot,
        db_manager: DatabaseSessionManager,
        knowledge_gap_service: KnowledgeGapService,
        admin_telegram_ids: Iterable[int] | None = None,
    ) -> None:
        self.bot = bot
        self.db_manager = db_manager
        self.knowledge_gap_service = knowledge_gap_service
        self.admin_telegram_ids = set(admin_telegram_ids) if admin_telegram_ids else set()

    @staticmethod
    def is_command(text: str | None) -> bool:
        """Check if message string starts with a slash command."""
        return bool(text and text.startswith("/"))

    def is_admin(self, telegram_id: int) -> bool:
        """Check if the given Telegram ID has admin privileges."""
        return telegram_id in self.admin_telegram_ids

    @staticmethod
    def format_number(n: int | float) -> str:
        """Format token counts compactly with K, M, B suffixes."""
        num = float(n)
        if num < 1000:
            return str(int(num))
        if num < 1_000_000:
            return f"{num / 1000.0:.1f}K"
        if num < 1_000_000_000:
            return f"{num / 1_000_000.0:.1f}M"
        return f"{num / 1_000_000_000.0:.1f}B"

    async def send_help(self, chat_id: int) -> None:
        """Send the standard help manual to user."""
        await self.bot.send_message(chat_id=chat_id, text=get_message("bot.help"))

    async def send_unknown_command(self, chat_id: int) -> None:
        """Send unknown command fallback notice."""
        await self.bot.send_message(chat_id=chat_id, text=get_message("bot.unknown.command"))

    async def handle_admin_command(self, chat_id: int, telegram_id: int, text: str) -> bool:
        """Execute admin command if sender is authorized and text matches."""
        if not self.is_admin(telegram_id):
            return False

        if text.startswith("/stats"):
            await self.handle_stats(chat_id, text)
            return True

        if text == "/gaps":
            await self.handle_gaps(chat_id)
            return True

        return False

    async def handle_stats(self, chat_id: int, command: str) -> None:
        """Parse /stats arguments and display leaderboard or per-user consumption."""
        parts = command.split()
        if len(parts) == 2:
            try:
                num = int(parts[1])
                if num <= self.STATS_ID_THRESHOLD:
                    limit = max(1, min(num, self.STATS_ID_THRESHOLD))
                    await self.show_top_stats(chat_id, limit)
                else:
                    await self.show_user_stats(chat_id, num)
                return
            except ValueError:
                pass

        await self.show_top_stats(chat_id, self.DEFAULT_STATS_LIMIT)

    async def show_top_stats(self, chat_id: int, limit: int) -> None:
        """Query top LLM token spenders and render leaderboard."""
        async with self.db_manager.session() as session:
            stmt = (
                select(
                    LlmTokenUsage.telegram_id,
                    func.sum(func.coalesce(LlmTokenUsage.total_tokens, 0)).label("total_tokens"),
                    func.sum(func.coalesce(LlmTokenUsage.prompt_tokens, 0)).label("prompt_tokens"),
                    func.sum(func.coalesce(LlmTokenUsage.completion_tokens, 0)).label("completion_tokens"),
                    func.count().label("request_count"),
                )
                .group_by(LlmTokenUsage.telegram_id)
                .order_by(func.sum(func.coalesce(LlmTokenUsage.total_tokens, 0)).desc())
                .limit(limit)
            )
            res = await session.execute(stmt)
            rows = res.fetchall()

        if not rows:
            await self.bot.send_message(chat_id=chat_id, text=get_message("bot.stats.empty"))
            return

        lines = [get_message("bot.stats.top.header", limit)]
        for rank, row in enumerate(rows, start=1):
            name = await self.resolve_user_name(row.telegram_id)
            lines.append(
                get_message(
                    "bot.stats.top.row",
                    rank,
                    name,
                    self.format_number(row.total_tokens or 0),
                    row.request_count,
                )
            )

        await self.bot.send_message(chat_id=chat_id, text="\n".join(lines))

    async def show_user_stats(self, chat_id: int, telegram_id: int) -> None:
        """Query token consumption for a single user ID."""
        async with self.db_manager.session() as session:
            stmt = (
                select(
                    LlmTokenUsage.telegram_id,
                    func.sum(func.coalesce(LlmTokenUsage.total_tokens, 0)).label("total_tokens"),
                    func.sum(func.coalesce(LlmTokenUsage.prompt_tokens, 0)).label("prompt_tokens"),
                    func.sum(func.coalesce(LlmTokenUsage.completion_tokens, 0)).label("completion_tokens"),
                    func.count().label("request_count"),
                )
                .where(LlmTokenUsage.telegram_id == telegram_id)
                .group_by(LlmTokenUsage.telegram_id)
            )
            res = await session.execute(stmt)
            row = res.fetchone()

        name = await self.resolve_user_name(telegram_id)
        if row is None or (row.request_count or 0) == 0:
            await self.bot.send_message(chat_id=chat_id, text=get_message("bot.stats.no.data", name))
            return

        text = get_message(
            "bot.stats.user",
            name,
            row.request_count,
            self.format_number(row.prompt_tokens or 0),
            self.format_number(row.completion_tokens or 0),
            self.format_number(row.total_tokens or 0),
        )
        await self.bot.send_message(chat_id=chat_id, text=text)

    async def handle_gaps(self, chat_id: int) -> None:
        """Query top detected knowledge gaps and render summary."""
        gaps = await self.knowledge_gap_service.get_top_gaps()
        if not gaps:
            await self.bot.send_message(chat_id=chat_id, text=get_message("bot.gaps.empty"))
            return

        lines = [get_message("bot.gaps.header")]
        for rank, gap in enumerate(gaps, start=1):
            lines.append(
                get_message(
                    "bot.gaps.row",
                    rank,
                    gap.gap_count,
                    gap.user_query,
                    gap.trigger_reason or "UNKNOWN",
                )
            )

        await self.bot.send_message(chat_id=chat_id, text="\n".join(lines))

    async def resolve_user_name(self, telegram_id: int | None) -> str:
        """Resolve username handle from user_names table or fallback to Telegram ID."""
        if telegram_id is None:
            return "Unknown"

        try:
            async with self.db_manager.session() as session:
                user = await session.get(User, telegram_id)
                if user is not None and user.username and str(user.username).strip():
                    return f"@{str(user.username).strip()} ({telegram_id})"
        except Exception as e:
            logger.debug("Failed to resolve username for %s: %s", telegram_id, e)

        return str(telegram_id)
