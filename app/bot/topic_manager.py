"""Thread-safe forum topic resolution & creation per user in the support supergroup."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram import Bot
from sqlalchemy import delete, select

from app.storage.database import DatabaseSessionManager
from app.storage.models import TopicMapping

logger = logging.getLogger(__name__)


class TopicManager:
    """Manages creation and mapping of dedicated forum topics for support conversations."""

    def __init__(
        self,
        db_manager: DatabaseSessionManager,
        bot: Bot,
        support_group_chat_id: int,
    ) -> None:
        self.db_manager = db_manager
        self.bot = bot
        self.support_group_chat_id = support_group_chat_id
        self._user_locks: dict[int, asyncio.Lock] = {}
        self._lock_users: dict[int, int] = {}
        self._global_lock = asyncio.Lock()

    @asynccontextmanager
    async def _user_lock(self, user_id: int) -> AsyncIterator[None]:
        """Serialise topic resolution per user without retaining a lock forever."""
        async with self._global_lock:
            lock = self._user_locks.get(user_id)
            if lock is None:
                lock = asyncio.Lock()
                self._user_locks[user_id] = lock
            self._lock_users[user_id] = self._lock_users.get(user_id, 0) + 1

        try:
            async with lock:
                yield
        finally:
            async with self._global_lock:
                remaining = self._lock_users.get(user_id, 1) - 1
                if remaining <= 0:
                    self._lock_users.pop(user_id, None)
                    self._user_locks.pop(user_id, None)
                else:
                    self._lock_users[user_id] = remaining

    async def resolve_topic_id(self, user_id: int, user_name: str | None) -> int | None:
        """Find existing forum topic ID for user or create a new one."""
        async with self._user_lock(user_id):
            async with self.db_manager.session() as session:
                result = await session.execute(
                    select(TopicMapping).where(TopicMapping.user_id == user_id)
                )
                mapping = result.scalar_one_or_none()
                if mapping is not None:
                    return mapping.topic_id

            return await self._create_topic(user_id, user_name)

    async def recreate_stale_topic(
        self,
        user_id: int,
        user_name: str | None,
        stale_topic_id: int | None,
    ) -> int | None:
        """Delete stale topic mapping if it matches stale_topic_id and create a new topic."""
        async with self._user_lock(user_id):
            async with self.db_manager.session() as session:
                result = await session.execute(
                    select(TopicMapping).where(TopicMapping.user_id == user_id)
                )
                existing = result.scalar_one_or_none()
                if existing is not None and existing.topic_id == stale_topic_id:
                    await session.execute(
                        delete(TopicMapping).where(TopicMapping.user_id == user_id)
                    )
                    logger.info(
                        "Deleted stale topic mapping %s for user %d", stale_topic_id, user_id
                    )

            return await self._create_topic(user_id, user_name)

    async def _create_topic(self, user_id: int, user_name: str | None) -> int | None:
        """Invoke Telegram API to create a new forum topic and record mapping in DB."""
        topic_name = self._build_topic_name(user_id, user_name)
        logger.info("Creating forum topic for user %d: %s", user_id, topic_name)

        try:
            response = await self.bot.create_forum_topic(
                chat_id=self.support_group_chat_id,
                name=topic_name,
            )
            topic_id = getattr(response, "message_thread_id", None)
            if topic_id is None and hasattr(response, "forum_topic"):
                topic_id = getattr(response.forum_topic, "message_thread_id", None)

            if topic_id is not None:
                async with self.db_manager.session() as session:
                    mapping = TopicMapping(
                        user_id=user_id,
                        topic_id=topic_id,
                        user_name=user_name,
                    )
                    session.add(mapping)
                logger.info("Created topic %d for user %d", topic_id, user_id)
                return topic_id

            logger.error("Failed to create topic for user %d: empty message_thread_id", user_id)
            return None
        except Exception as e:
            logger.error("Error creating topic for user %d: %s", user_id, e, exc_info=True)
            return None

    def _build_topic_name(self, user_id: int, user_name: str | None) -> str:
        """Format topic title with username/name or fallback to User ID."""
        if user_name is not None and str(user_name).strip():
            return f"{str(user_name).strip()} (ID: {user_id})"
        return f"User {user_id}"
