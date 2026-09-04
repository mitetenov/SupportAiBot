"""Keeps the 'typing…' status alive for the duration of an ongoing request."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from aiogram import Bot

from app.logging_config import TRACE

logger = logging.getLogger(__name__)


class TypingSession:
    """Async session sending typing action periodically until closed or context exits."""

    def __init__(self, bot: Bot, chat_id: int, refresh_seconds: float = 4.0) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.refresh_seconds = refresh_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        # Set by TypingIndicator so the session drops out of its registry.
        self.on_close: Callable[[TypingSession], None] | None = None

    async def _run(self) -> None:
        while not self._stopped:
            try:
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "Telegram API send_chat_action: chat_id=%s, action=typing",
                        self.chat_id,
                    )
                await self.bot.send_chat_action(chat_id=self.chat_id, action="typing")
            except Exception as e:
                logger.debug("Failed to send typing action to %d: %s", self.chat_id, e)
            try:
                await asyncio.sleep(self.refresh_seconds)
            except asyncio.CancelledError:
                break

    def start(self) -> TypingSession:
        """Start the background typing loop task."""
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self._run())
        return self

    def close(self) -> None:
        """Stop the typing loop immediately."""
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
        if self.on_close is not None:
            self.on_close(self)

    async def stop(self) -> None:
        """Async stop and await cancellation cleanup."""
        self.close()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def __aenter__(self) -> TypingSession:
        return self.start()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


class TypingIndicator:
    """Manages active typing sessions for chats."""

    def __init__(self, bot: Bot, refresh_seconds: float = 4.0) -> None:
        self.bot = bot
        self.refresh_seconds = refresh_seconds
        # A session deregisters itself on close, so this tracks what is actually
        # running rather than everything that ever ran.
        self._active_sessions: set[TypingSession] = set()

    def _new_session(self, chat_id: int) -> TypingSession:
        session = TypingSession(self.bot, chat_id, self.refresh_seconds)
        session.on_close = self._active_sessions.discard
        self._active_sessions.add(session)
        return session

    def start(self, chat_id: int) -> TypingSession:
        """Start a typing session for the given chat ID."""
        return self._new_session(chat_id).start()

    def session(self, chat_id: int) -> TypingSession:
        """Create a typing session instance (can be used with async with)."""
        return self._new_session(chat_id)

    def shutdown(self) -> None:
        """Cancel and clean up all active typing sessions."""
        for session in list(self._active_sessions):
            session.close()
        self._active_sessions.clear()
