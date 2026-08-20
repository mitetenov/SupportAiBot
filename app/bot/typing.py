"""Keeps the 'typing…' status alive for the duration of an ongoing request."""

import asyncio
import logging
from typing import Any

from aiogram import Bot

logger = logging.getLogger(__name__)


class TypingSession:
    """Async session sending typing action periodically until closed or context exits."""

    def __init__(self, bot: Bot, chat_id: int, refresh_seconds: float = 4.0) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.refresh_seconds = refresh_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self.bot.send_chat_action(chat_id=self.chat_id, action="typing")
            except Exception as e:
                logger.debug("Failed to send typing action to %d: %s", self.chat_id, e)
            try:
                await asyncio.sleep(self.refresh_seconds)
            except asyncio.CancelledError:
                break

    def start(self) -> "TypingSession":
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

    async def stop(self) -> None:
        """Async stop and await cancellation cleanup."""
        self.close()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def __aenter__(self) -> "TypingSession":
        return self.start()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


class TypingIndicator:
    """Manages active typing sessions for chats."""

    def __init__(self, bot: Bot, refresh_seconds: float = 4.0) -> None:
        self.bot = bot
        self.refresh_seconds = refresh_seconds
        self._active_sessions: list[TypingSession] = []

    def start(self, chat_id: int) -> TypingSession:
        """Start a typing session for the given chat ID."""
        session = TypingSession(self.bot, chat_id, self.refresh_seconds)
        self._active_sessions.append(session)
        return session.start()

    def session(self, chat_id: int) -> TypingSession:
        """Create a typing session instance (can be used with async with)."""
        session = TypingSession(self.bot, chat_id, self.refresh_seconds)
        self._active_sessions.append(session)
        return session

    def shutdown(self) -> None:
        """Cancel and clean up all active typing sessions."""
        for session in self._active_sessions:
            session.close()
        self._active_sessions.clear()
