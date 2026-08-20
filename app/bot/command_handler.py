"""Handles bot slash commands (/start, /help, /operator, /stats, /gaps)."""

import logging
from collections.abc import Iterable
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select

from app.bot.sender import TelegramMessageSender
from app.constants import get_message
from app.rag.knowledge_gaps import KnowledgeGapService
from app.storage.database import DatabaseSessionManager
from app.storage.models import LlmTokenUsage, User

logger = logging.getLogger(__name__)


class SupportCommandHandler:
    """Processes user slash commands and administrative stats/gap inspections."""

    STATS_ID_THRESHOLD: int = 100
    DEFAULT_STATS_LIMIT: int = 10
    #: Sub-command that wipes whichever table the command belongs to.
    CLEAR_ARGUMENT: str = "clear"
    MAX_ERROR_LENGTH: int = 300

    def __init__(
        self,
        sender: TelegramMessageSender,
        db_manager: DatabaseSessionManager,
        knowledge_gap_service: KnowledgeGapService,
        admin_telegram_ids: Iterable[int] | None = None,
    ) -> None:
        self.sender = sender
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
        await self.sender.send(chat_id, get_message("bot.help"))

    async def send_unknown_command(self, chat_id: int) -> None:
        """Send unknown command fallback notice."""
        await self.sender.send(chat_id, get_message("bot.unknown.command"))

    async def handle_admin_command(self, chat_id: int, telegram_id: int, text: str) -> bool:
        """Execute admin command if sender is authorized and text matches."""
        if not self.is_admin(telegram_id):
            return False

        if text.startswith("/stats"):
            await self.handle_stats(chat_id, text)
            return True

        if text.startswith("/gaps"):
            await self.handle_gaps_command(chat_id, text)
            return True

        return False

    async def handle_stats(self, chat_id: int, command: str) -> None:
        """Parse /stats arguments and display leaderboard or per-user consumption."""
        parts = command.split()
        if len(parts) == 2 and parts[1].lower() == self.CLEAR_ARGUMENT:
            await self.clear_stats(chat_id)
            return

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
                    func.sum(func.coalesce(LlmTokenUsage.completion_tokens, 0)).label(
                        "completion_tokens"
                    ),
                    func.count().label("request_count"),
                )
                .group_by(LlmTokenUsage.telegram_id)
                .order_by(func.sum(func.coalesce(LlmTokenUsage.total_tokens, 0)).desc())
                .limit(limit)
            )
            res = await session.execute(stmt)
            rows = res.fetchall()

        if not rows:
            await self.sender.send(chat_id, get_message("bot.stats.empty"))
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

        await self.sender.send(chat_id, "\n".join(lines))

    async def show_user_stats(self, chat_id: int, telegram_id: int) -> None:
        """Query token consumption for a single user ID."""
        async with self.db_manager.session() as session:
            stmt = (
                select(
                    LlmTokenUsage.telegram_id,
                    func.sum(func.coalesce(LlmTokenUsage.total_tokens, 0)).label("total_tokens"),
                    func.sum(func.coalesce(LlmTokenUsage.prompt_tokens, 0)).label("prompt_tokens"),
                    func.sum(func.coalesce(LlmTokenUsage.completion_tokens, 0)).label(
                        "completion_tokens"
                    ),
                    func.count().label("request_count"),
                )
                .where(LlmTokenUsage.telegram_id == telegram_id)
                .group_by(LlmTokenUsage.telegram_id)
            )
            res = await session.execute(stmt)
            row = res.fetchone()

        name = await self.resolve_user_name(telegram_id)
        if row is None or (row.request_count or 0) == 0:
            await self.sender.send(chat_id, get_message("bot.stats.no.data", name))
            return

        text = get_message(
            "bot.stats.user",
            name,
            row.request_count,
            self.format_number(row.prompt_tokens or 0),
            self.format_number(row.completion_tokens or 0),
            self.format_number(row.total_tokens or 0),
        )
        await self.sender.send(chat_id, text)

    async def handle_gaps_command(self, chat_id: int, command: str) -> None:
        """Route /gaps to the report or, with the clear argument, to the wipe."""
        parts = command.split()
        if len(parts) == 2 and parts[1].lower() == self.CLEAR_ARGUMENT:
            await self.clear_gaps(chat_id)
            return
        await self.handle_gaps(chat_id)

    async def clear_gaps(self, chat_id: int) -> None:
        """Delete every recorded knowledge gap and report the count."""
        try:
            removed = await self.knowledge_gap_service.clear_all()
        except Exception as e:
            logger.error("Failed to clear knowledge gaps: %s", e, exc_info=True)
            await self.sender.send(chat_id, get_message("bot.clear.failed", self._describe(e)))
            return
        await self.sender.send(chat_id, get_message("bot.gaps.cleared", removed))

    async def clear_stats(self, chat_id: int) -> None:
        """Delete every recorded token usage row and report the count."""
        try:
            async with self.db_manager.session() as session:
                # Only a cursor result carries the row count a DELETE reports.
                result = cast(CursorResult[Any], await session.execute(delete(LlmTokenUsage)))
            removed = int(result.rowcount or 0)
        except Exception as e:
            logger.error("Failed to clear token usage stats: %s", e, exc_info=True)
            await self.sender.send(chat_id, get_message("bot.clear.failed", self._describe(e)))
            return
        logger.info("Cleared token usage stats: %d rows removed", removed)
        await self.sender.send(chat_id, get_message("bot.stats.cleared", removed))

    @classmethod
    def _describe(cls, cause: Exception) -> str:
        """A short, bounded description of a failure for the admin chat."""
        message = str(cause) or cause.__class__.__name__
        if len(message) > cls.MAX_ERROR_LENGTH:
            message = message[: cls.MAX_ERROR_LENGTH] + "..."
        return message

    async def handle_gaps(self, chat_id: int) -> None:
        """Query top detected knowledge gaps and render summary."""
        gaps = await self.knowledge_gap_service.get_top_gaps()
        if not gaps:
            await self.sender.send(chat_id, get_message("bot.gaps.empty"))
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

        await self.sender.send(chat_id, "\n".join(lines))

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
