"""In-memory LRU chat history service with bounded deque and async DB persistence."""

import logging
import time
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from app.llm.rejection import is_rejection
from app.logging_config import TRACE
from app.storage.database import DatabaseSessionManager
from app.storage.models import ChatMessage

logger = logging.getLogger(__name__)


class ChatHistoryService:
    """Manages fast in-memory user conversation history backed by persistent storage."""

    def __init__(
        self,
        db_manager: DatabaseSessionManager | None = None,
        max_messages: int = 20,
        ttl_days: int = 7,
    ) -> None:
        self.db_manager = db_manager
        self.max_messages = max_messages
        self.ttl_days = ttl_days

        self._histories: dict[int, deque[dict[str, str]]] = {}
        self._last_activity: dict[int, float] = {}
        self._loaded_from_db: set[int] = set()
        self._rejected_faq_questions: dict[int, set[str]] = {}

    async def get_history(self, user_id: int) -> list[dict[str, str]]:
        """Retrieve chronological history for a user, loading from DB if not in memory."""
        if user_id not in self._loaded_from_db:
            await self._load_from_database(user_id)

        history = self._histories.get(user_id)
        if not history:
            return []

        self._last_activity[user_id] = time.time()
        return [{"role": msg["role"], "content": msg["content"]} for msg in history]

    async def add_user_message(self, user_id: int, text: str | None) -> None:
        """Append a user message, trim history to max limit, and persist asynchronously."""
        if text is None or not text.strip():
            return
        await self._append(user_id, "user", text.strip())

    async def add_assistant_message(self, user_id: int, text: str | None) -> None:
        """Append an assistant response, trim history to max limit, and persist asynchronously."""
        if text is None or not text.strip():
            return
        await self._append(user_id, "assistant", text.strip())

    async def _append(self, user_id: int, role: str, content: str) -> None:
        """Internal helper to append message to in-memory deque and save to DB."""
        self._last_activity[user_id] = time.time()
        if user_id not in self._histories:
            self._histories[user_id] = deque(maxlen=self.max_messages)
        self._loaded_from_db.add(user_id)

        self._histories[user_id].append({"role": role, "content": content})

        if self.db_manager is not None:
            try:
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "ChatHistory persisting message: user=%d, role=%s, content=%s",
                        user_id,
                        role,
                        content,
                    )
                async with self.db_manager.session() as session:
                    msg = ChatMessage(telegram_id=user_id, role=role, content=content)
                    session.add(msg)
            except Exception as e:
                logger.error(
                    "Failed to persist chat message (component=ChatHistoryService, operation=_append, error_class=%s)",
                    type(e).__name__,
                )
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "Failed to persist chat message for user %d: %s",
                        user_id,
                        e,
                        exc_info=True,
                    )

    async def to_gemini_contents(self, user_id: int) -> list[dict[str, Any]]:
        """Convert user history to Gemini API format with 'user' and 'model' roles."""
        history = await self.get_history(user_id)
        if not history:
            return []

        contents: list[dict[str, Any]] = []
        for msg in history:
            gemini_role = "model" if msg["role"] == "assistant" else "user"
            contents.append(
                {
                    "role": gemini_role,
                    "parts": [{"text": msg["content"]}],
                }
            )
        return contents

    def get_last_user_message(self, user_id: int) -> str | None:
        """Return the most recent text message sent by the user, or None."""
        history = self._histories.get(user_id)
        if not history:
            return None

        for msg in reversed(history):
            if msg.get("role") == "user":
                return msg.get("content")
        return None

    def get_rejected_faq_questions(self, user_id: int) -> set[str]:
        """Return set of FAQ questions rejected during the current support turn."""
        return set(self._rejected_faq_questions.get(user_id, set()))

    def add_rejected_faq_questions(self, user_id: int, questions: Iterable[str] | None) -> None:
        """Record rejected FAQ questions for exclusion in follow-up queries."""
        if not questions:
            return
        self._rejected_faq_questions.setdefault(user_id, set()).update(questions)

    def clear_rejected_faqs_if_new_topic(self, user_id: int, user_message: str | None) -> None:
        """Reset rejected FAQ questions if the incoming message is a new topic, not a rejection."""
        if user_message is None or not user_message.strip():
            return
        if not is_rejection(user_message):
            self._rejected_faq_questions.pop(user_id, None)

    async def clear(self, user_id: int) -> None:
        """Clear user history from both in-memory cache and persistent database."""
        self._histories.pop(user_id, None)
        self._last_activity.pop(user_id, None)
        self._loaded_from_db.discard(user_id)
        self._rejected_faq_questions.pop(user_id, None)

        if self.db_manager is not None:
            try:
                async with self.db_manager.session() as session:
                    stmt = delete(ChatMessage).where(ChatMessage.telegram_id == user_id)
                    if logger.isEnabledFor(TRACE):
                        logger.log(TRACE, "ChatHistory clear SQL: %s, user=%d", stmt, user_id)
                    await session.execute(stmt)
                    if logger.isEnabledFor(TRACE):
                        logger.log(TRACE, "Chat history deleted from DB for user %d", user_id)
            except Exception as e:
                logger.error(
                    "Failed to delete chat history from DB (component=ChatHistoryService, operation=clear, error_class=%s)",
                    type(e).__name__,
                )
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "Failed to delete chat history from DB for user %d: %s",
                        user_id,
                        e,
                        exc_info=True,
                    )

    async def evict_stale_entries(self, ttl_days: int | None = None) -> None:
        """Evict expired records from DB and clear stale entries from memory."""
        days = ttl_days if ttl_days is not None else self.ttl_days
        cutoff_dt = datetime.now(UTC) - timedelta(days=days)

        if self.db_manager is not None:
            try:
                async with self.db_manager.session() as session:
                    stmt = delete(ChatMessage).where(ChatMessage.created_at < cutoff_dt)
                    if logger.isEnabledFor(TRACE):
                        logger.log(TRACE, "ChatHistory evict SQL: %s, cutoff=%s", stmt, cutoff_dt)
                    await session.execute(stmt)
            except Exception as e:
                logger.error(
                    "Failed to evict stale chat messages (component=ChatHistoryService, operation=evict_stale_entries, error_class=%s)",
                    type(e).__name__,
                )
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE, "Failed to evict stale chat messages exception: %s", e, exc_info=True
                    )

        memory_cutoff = time.time() - (days * 86400)
        stale_users = [
            uid for uid, last_seen in self._last_activity.items() if last_seen < memory_cutoff
        ]
        for uid in stale_users:
            self._histories.pop(uid, None)
            self._last_activity.pop(uid, None)
            self._loaded_from_db.discard(uid)
            self._rejected_faq_questions.pop(uid, None)
            if logger.isEnabledFor(TRACE):
                logger.log(TRACE, "Evicted stale in-memory history for user %d", uid)

    async def _load_from_database(self, user_id: int) -> None:
        """Load the most recent N messages from DB in chronological order."""
        if self.db_manager is None:
            self._histories.setdefault(user_id, deque(maxlen=self.max_messages))
            self._loaded_from_db.add(user_id)
            return

        try:
            async with self.db_manager.session() as session:
                stmt = (
                    select(ChatMessage)
                    .where(ChatMessage.telegram_id == user_id)
                    .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                    .limit(self.max_messages)
                )
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "Storage select chat_history: user_id=%d, SQL=%s",
                        user_id,
                        stmt,
                    )
                result = await session.execute(stmt)
                messages = list(result.scalars().all())
                # Result is newest-first; reverse to chronological order
                messages.reverse()
                self._histories[user_id] = deque(
                    [{"role": msg.role, "content": msg.content} for msg in messages],
                    maxlen=self.max_messages,
                )
                self._loaded_from_db.add(user_id)
                self._last_activity[user_id] = time.time()
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "ChatHistory loaded %d messages for user %d: %s",
                        len(messages),
                        user_id,
                        [{"role": m.role, "content": m.content} for m in messages],
                    )
        except Exception as e:
            logger.error(
                "Failed to load chat history from DB (component=ChatHistoryService, operation=_load_from_database, error_class=%s)",
                type(e).__name__,
            )
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Failed to load chat history from DB for user %d: %s",
                    user_id,
                    e,
                    exc_info=True,
                )
            self._histories[user_id] = deque(maxlen=self.max_messages)
            self._loaded_from_db.add(user_id)
