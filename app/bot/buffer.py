"""Coalesces messages a user sends in quick succession into a single batch."""

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BufferedMessage:
    """One Telegram message waiting to be merged."""

    message: Any
    text: str | None = None
    base64_image: str | None = None
    mime_type: str | None = None

    @classmethod
    def from_text(cls, message: Any, text: str) -> BufferedMessage:
        """Factory method for creating a text-only buffered message."""
        return cls(message=message, text=text, base64_image=None, mime_type=None)


@dataclass(frozen=True)
class MessageBatch:
    """Consecutive messages merged into one request."""

    last_message: Any
    user: Any
    text: str
    message_ids: list[int]
    base64_image: str | None = None
    mime_type: str | None = None

    @classmethod
    def of(cls, messages: list[BufferedMessage]) -> MessageBatch:
        """Create a merged batch from a list of buffered messages."""
        if not messages:
            raise ValueError("Cannot create MessageBatch from empty message list")

        last = messages[-1]
        ids = [
            getattr(m.message, "message_id", None) or getattr(m.message, "id", 0) for m in messages
        ]

        text_parts = [m.text for m in messages if m.text and str(m.text).strip()]
        merged_text = "\n".join(text_parts)

        # At most one image per batch
        with_image = next((m for m in messages if m.base64_image is not None), None)

        user = (
            getattr(last.message, "from_user", None)
            or getattr(last.message, "from", None)
            or getattr(last.message, "user", None)
        )

        return cls(
            last_message=last.message,
            user=user,
            text=merged_text,
            message_ids=ids,
            base64_image=with_image.base64_image if with_image else None,
            mime_type=with_image.mime_type if with_image else None,
        )

    def has_image(self) -> bool:
        """Return True if batch contains an image."""
        return self.base64_image is not None

    def size(self) -> int:
        """Return number of merged messages."""
        return len(self.message_ids)


@dataclass
class _Batch:
    messages: list[BufferedMessage] = field(default_factory=list)
    generation: int = 0
    handle: asyncio.TimerHandle | None = None


class UserMessageBuffer:
    """Coalesces messages a user sends in quick succession into a single batch."""

    def __init__(
        self,
        window_ms: int = 2500,
        max_messages: int = 5,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.window_seconds = window_ms / 1000.0
        self.max_messages = max_messages
        self._loop = loop
        self._pending: dict[int, _Batch] = {}
        # Strong references to in-flight dispatches. Without them the event loop
        # only holds a weak reference and the task can be garbage-collected
        # mid-answer; it is also the only place an escaped exception is visible.
        self._inflight: set[asyncio.Task[Any]] = set()

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and not self._loop.is_closed():
            return self._loop
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    def submit(
        self,
        user_id: int,
        message: BufferedMessage,
        sink: Callable[[MessageBatch], Any],
    ) -> None:
        """Adds message to the user's pending batch and (re)arms the flush timer."""
        loop = self._get_loop()

        batch = self._pending.get(user_id)
        if batch is None:
            batch = _Batch()
            self._pending[user_id] = batch

        if batch.handle is not None:
            batch.handle.cancel()
            batch.handle = None

        batch.messages.append(message)
        batch.generation += 1

        flush_now = len(batch.messages) >= self.max_messages

        if not flush_now:
            gen = batch.generation
            batch.handle = loop.call_later(
                self.window_seconds,
                lambda: self._flush(user_id, gen, sink),
            )
        else:
            self._flush(user_id, -1, sink)

    def _flush(
        self,
        user_id: int,
        generation: int,
        sink: Callable[[MessageBatch], Any],
    ) -> None:
        """Flushes user's pending message batch if generation matches or -1."""
        batch = self._pending.get(user_id)
        if batch is None or not batch.messages:
            return

        if generation >= 0 and batch.generation != generation:
            return

        if batch.handle is not None:
            batch.handle.cancel()
            batch.handle = None

        del self._pending[user_id]

        try:
            message_batch = MessageBatch.of(batch.messages)
            res = sink(message_batch)
            if inspect.isawaitable(res) and not isinstance(res, asyncio.Task):
                self._track(asyncio.ensure_future(res), user_id)
            elif isinstance(res, asyncio.Task):
                self._track(res, user_id)
        except Exception as e:
            logger.error("Failed to dispatch buffered messages for user %d: %s", user_id, e)

    def _track(self, task: asyncio.Task[Any], user_id: int) -> None:
        """Hold the task until it finishes, and surface anything it raised."""
        self._inflight.add(task)

        def _done(finished: asyncio.Task[Any]) -> None:
            self._inflight.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                logger.error(
                    "Unhandled error while answering user %d: %s", user_id, error, exc_info=error
                )

        task.add_done_callback(_done)

    def shutdown(self) -> None:
        """Cancel all pending flush timers and any dispatch still in flight."""
        for batch in self._pending.values():
            if batch.handle is not None:
                batch.handle.cancel()
                batch.handle = None
        self._pending.clear()
        for task in list(self._inflight):
            task.cancel()
        self._inflight.clear()
