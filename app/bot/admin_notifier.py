"""Error reporting and alert dispatching to support supergroup."""

import logging
import time
from dataclasses import dataclass

from aiogram import Bot

from app.constants import get_message
from app.logging_config import TRACE

logger = logging.getLogger(__name__)


@dataclass
class _Throttled:
    """When one alert context last went out, and what was swallowed since."""

    sent_at: float
    suppressed: int = 0


class AdminNotifier:
    """Dispatches diagnostic failure alerts to the administrators' support supergroup."""

    MAX_ERROR_LENGTH: int = 2000

    #: How long one alert context stays quiet after it has been reported.
    #: A failure that repeats — a revoked API key, an endpoint answering 500 —
    #: repeats once per ticket per sweep, and a support group buried under
    #: hundreds of identical alerts is a support group that stops reading them.
    THROTTLE_WINDOW_SECONDS: float = 600.0

    def __init__(self, bot: Bot, support_group_chat_id: int) -> None:
        self.bot = bot
        self.support_group_chat_id = support_group_chat_id
        # context -> when it was last sent. Deliberately per-context and not
        # global: an unrelated failure elsewhere must still get through.
        self._throttled: dict[str, _Throttled] = {}

    async def notify_error(
        self,
        context: str,
        user_id: int | None = None,
        error: Exception | None = None,
    ) -> None:
        """Send a silent diagnostic alert to the support group.

        Repeats of the same `context` inside `THROTTLE_WINDOW_SECONDS` are
        counted rather than sent; the next alert that does go out says how many
        it stands for, so nothing is hidden — only compressed.
        """
        suppressed = self._claim_slot(context)
        if suppressed is None:
            return

        raw_msg = str(error) if error is not None else "null"
        error_message = (
            raw_msg[: self.MAX_ERROR_LENGTH] if len(raw_msg) > self.MAX_ERROR_LENGTH else raw_msg
        )

        lines = [get_message("admin.error.prefix"), context]
        if user_id is not None:
            lines.append(f"User: {user_id}")
        if suppressed > 0:
            lines.append(get_message("admin.error.suppressed", suppressed))
        lines.append("")
        lines.append(error_message)

        text = "\n".join(lines)
        try:
            await self.bot.send_message(
                chat_id=self.support_group_chat_id,
                text=text,
                disable_notification=True,
            )
        except Exception as e:
            logger.error(
                "Failed to send admin error notification (error_class=%s)", type(e).__name__
            )
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE, "Failed to send admin error notification exception: %s", e, exc_info=True
                )

    def _claim_slot(self, context: str) -> int | None:
        """How many repeats this alert stands for, or None to stay quiet."""
        now = time.monotonic()
        self._evict(now)

        entry = self._throttled.get(context)
        if entry is not None and now - entry.sent_at < self.THROTTLE_WINDOW_SECONDS:
            entry.suppressed += 1
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "Admin alert suppressed (%d since the last one): %s",
                    entry.suppressed,
                    context,
                )
            return None

        suppressed = entry.suppressed if entry is not None else 0
        self._throttled[context] = _Throttled(sent_at=now)
        return suppressed

    def _evict(self, now: float) -> None:
        """Forget contexts nobody has reported in a long while.

        Ticket ids and user ids end up in context strings, so the map would
        otherwise grow for as long as the process runs. Twice the window, not
        once: an entry expiring right now still has to hand its count to the
        alert that replaces it.
        """
        cutoff = now - 2 * self.THROTTLE_WINDOW_SECONDS
        for key in [k for k, v in self._throttled.items() if v.sent_at < cutoff]:
            del self._throttled[key]
