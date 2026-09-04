"""Ordered cross-provider fallback without duplicating one chat turn's side effects."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from app.llm.base import (
    AbstractLlmClient,
    LlmProcessingException,
    LlmReply,
    LlmToolExecutionException,
    is_balance_exhaustion_message,
)
from app.logging_config import TRACE

logger = logging.getLogger(__name__)

# Request-size limits differ between providers/models. Retrying the same
# endpoint cannot fix 413, but the next configured target may accept the turn.
_FALLBACK_STATUS_CODES: frozenset[int] = frozenset(
    {401, 402, 403, 408, 413, 429, 500, 502, 503, 504}
)


class LlmFallbackExhaustedError(LlmProcessingException):
    """All configured providers failed in a way that permits failover."""

    def __init__(self) -> None:
        super().__init__(
            "All configured LLM providers are temporarily unavailable",
            "Сервис временно недоступен. Попробуйте позже.",
        )


def is_fallback_eligible(error: Exception) -> bool:
    """Return whether retrying the same turn at another provider is meaningful."""
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, LlmToolExecutionException):
            return False
        if isinstance(current, LlmProcessingException):
            if current.status_code in _FALLBACK_STATUS_CODES:
                return True
            if current.fallback_eligible or is_balance_exhaustion_message(str(current)):
                return True
            current = current.cause
            continue
        if isinstance(current, (httpx.TimeoutException, httpx.TransportError)):
            return True
        current = current.__cause__
    return False


class LlmFallbackClient:
    """Try configured clients in order while committing history exactly once."""

    def __init__(self, clients: Sequence[AbstractLlmClient]) -> None:
        if not clients:
            raise ValueError("LlmFallbackClient requires at least one provider client")
        self._clients = tuple(clients)

    def get_provider_name(self) -> str:
        """Return a safe display name for diagnostics without exposing configuration secrets."""
        return " → ".join(client.get_provider_name() for client in self._clients)

    def supports_images(self) -> bool:
        """Return whether any configured target can accept image input."""
        return any(client.supports_images() for client in self._clients)

    async def chat(self, user_message: str, telegram_user_id: int) -> LlmReply:
        """Run a text turn against each target until one produces a final reply."""
        return await self._respond(
            user_message, telegram_user_id, None, None, user_message, self._clients
        )

    async def chat_with_image(
        self,
        user_message: str,
        telegram_user_id: int,
        base64_image: str,
        mime_type: str | None = None,
    ) -> LlmReply:
        """Run an image turn only against providers that advertise image support."""
        image_clients = tuple(client for client in self._clients if client.supports_images())
        if not image_clients:
            raise LlmProcessingException(
                "No configured fallback provider supports images",
                "Настроенные модели не поддерживают обработку изображений. Опишите проблему текстом.",
            )
        history_message = user_message if user_message.strip() else "[Скриншот]"
        return await self._respond(
            user_message,
            telegram_user_id,
            base64_image,
            mime_type,
            history_message,
            image_clients,
        )

    async def _respond(
        self,
        user_message: str,
        telegram_user_id: int,
        base64_image: str | None,
        mime_type: str | None,
        history_message: str,
        candidates: Sequence[AbstractLlmClient],
    ) -> LlmReply:
        turn_state = await candidates[0].prepare_turn(user_message, telegram_user_id)
        for attempt, client in enumerate(candidates):
            turn_state.replay_completed_tool_results = attempt > 0
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "LlmFallbackClient: attempting provider %s (attempt %d/%d, replaying_tools=%s)",
                    client.get_provider_name(),
                    attempt + 1,
                    len(candidates),
                    turn_state.replay_completed_tool_results,
                )
            try:
                reply = await client.do_chat(
                    user_message,
                    telegram_user_id,
                    base64_image,
                    mime_type,
                    turn_state=turn_state,
                )
            except Exception as error:
                if not is_fallback_eligible(error):
                    raise
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "LlmFallbackClient: provider %s failed with error (%s): %s; falling back to next provider",
                        client.get_provider_name(),
                        type(error).__name__,
                        error,
                    )
                logger.warning(
                    "LLM provider %s is unavailable; trying the next configured target",
                    client.get_provider_name(),
                )
                continue

            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "LlmFallbackClient: provider %s succeeded for user %s",
                    client.get_provider_name(),
                    telegram_user_id,
                )
            await self._persist_success(client, telegram_user_id, history_message, reply)
            return reply
        raise LlmFallbackExhaustedError()

    @staticmethod
    async def _persist_success(
        client: AbstractLlmClient,
        telegram_user_id: int,
        history_message: str,
        reply: LlmReply,
    ) -> None:
        """Commit a successful turn once, after all fallback processing is complete."""
        await client.chat_history_service.add_user_message(telegram_user_id, history_message)
        await client.chat_history_service.add_assistant_message(telegram_user_id, reply.text)
        client.chat_history_service.add_rejected_faq_questions(
            telegram_user_id, reply.faq_context.questions()
        )
