from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

from app.llm.rejection import is_rejection
from app.rag.types import FaqContext
from app.storage.models import LlmTokenUsage

if TYPE_CHECKING:
    from app.llm.mcp_router import McpRouter
    from app.rag.service import FaqEmbeddingService
    from app.storage.chat_history import ChatHistoryService
    from app.storage.database import DatabaseSessionManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LlmReply:
    """A model reply together with the FAQ retrieval that produced it."""

    text: str
    faq_context: FaqContext = field(default_factory=lambda: FaqContext.EMPTY)


@dataclass(frozen=True)
class TokenUsage:
    """What one API call cost, in the provider-neutral shape we store."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ToolCall:
    """A tool call requested by the model."""

    name: str
    id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    thought_signature: str | None = None


@dataclass(frozen=True)
class LlmResponse:
    """Parsed model response carrying text, tool calls, and raw candidate parts."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_parts: list[dict[str, Any]] = field(default_factory=list)

    def has_tool_calls(self) -> bool:
        """Return True if response contains at least one tool call."""
        return bool(self.tool_calls)


class LlmProcessingException(Exception):
    """Exception raised when an LLM provider request or parsing fails."""

    def __init__(
        self,
        message: str,
        user_friendly_message: str = "Произошла ошибка при обработке запроса. Попробуйте позже.",
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.user_friendly_message = user_friendly_message
        self.cause = cause


@runtime_checkable
class LlmClient(Protocol):
    """Protocol for LLM client implementations."""

    async def chat(self, user_message: str, telegram_user_id: int) -> LlmReply:
        """Execute a conversational turn with text input."""
        ...

    async def chat_with_image(
        self,
        user_message: str,
        telegram_user_id: int,
        base64_image: str,
        mime_type: str | None = None,
    ) -> LlmReply:
        """Execute a conversational turn with image and optional text caption."""
        ...

    def supports_images(self) -> bool:
        """Return whether this provider supports visual image inputs."""
        ...


class AbstractLlmClient(ABC, LlmClient):
    """Template method coordinator for multi-turn conversations and tool calling."""

    MAX_TOOL_ITERATIONS: int = 5
    TOOL_RESULT_MAX_LOG_LENGTH: int = 2000

    # First-person "I will go and look" with nothing actually done. Anchored on
    # first-person endings so an imperative aimed at the user ("проверьте пинг")
    # never matches.
    BARE_PROMISE_PATTERN: re.Pattern[str] = re.compile(
        r"\b(проверю|проверим|проверяю|посмотрю|посмотрим|смотрю|гляну|глянем"
        r"|уточню|уточняю|запрошу|узнаю|выясню|сверюсь|сверю)\b",
        re.IGNORECASE | re.UNICODE,
    )

    #: A promise longer than this is treated as a real answer that merely
    #: mentions checking something, not as a bare stall.
    MAX_BARE_PROMISE_LENGTH: int = 220

    RETRY_NUDGE: str = (
        "Ты написал, что сейчас что-то проверишь, но не вызвал ни одного инструмента. "
        "Вызови нужный инструмент прямо сейчас и ответь пользователю по существу. "
        "Не описывай намерение — либо инструмент, либо готовый ответ."
    )

    FOLLOW_UP_PATTERN: re.Pattern[str] = re.compile(
        r"^(а|и|но|ну)\s"
        r"|^(да|нет|ага|угу|ок|окей|понял|поняла)\b"
        r"|\b(это|этот|эта|туда|там|тут|оно|его|её|нём|ним|такое)\b"
        r"|^(айфон|iphone|андроид|android|винда|windows|макбук|мак|mac|linux|tv|телевизор)\b",
        re.IGNORECASE | re.UNICODE,
    )

    def __init__(
        self,
        mcp_router: McpRouter,
        chat_history_service: ChatHistoryService,
        faq_embedding_service: FaqEmbeddingService,
        db_manager: DatabaseSessionManager | None = None,
    ) -> None:
        self.mcp_router = mcp_router
        self.chat_history_service = chat_history_service
        self.faq_embedding_service = faq_embedding_service
        self.db_manager = db_manager

    def supports_images(self) -> bool:
        """Default image support is False unless overridden by provider."""
        return False

    async def chat(self, user_message: str, telegram_user_id: int) -> LlmReply:
        """Execute text conversation."""
        return await self._respond(user_message, telegram_user_id, None, None, user_message)

    async def chat_with_image(
        self,
        user_message: str,
        telegram_user_id: int,
        base64_image: str,
        mime_type: str | None = None,
    ) -> LlmReply:
        """Execute multimodal conversation with image input."""
        if not self.supports_images():
            raise LlmProcessingException(
                "Image not supported",
                f"{self.get_provider_name()} не поддерживает обработку изображений. Опишите проблему текстом.",
            )
        history_message = user_message if user_message and user_message.strip() else "[Скриншот]"
        return await self._respond(
            user_message, telegram_user_id, base64_image, mime_type, history_message
        )

    async def _respond(
        self,
        user_message: str,
        telegram_user_id: int,
        base64_image: str | None,
        mime_type: str | None,
        history_message: str,
    ) -> LlmReply:
        self.chat_history_service.clear_rejected_faqs_if_new_topic(telegram_user_id, user_message)
        reply = await self.do_chat(user_message, telegram_user_id, base64_image, mime_type)

        await self.chat_history_service.add_user_message(telegram_user_id, history_message)
        await self.chat_history_service.add_assistant_message(telegram_user_id, reply.text)
        self.chat_history_service.add_rejected_faq_questions(
            telegram_user_id, reply.faq_context.questions()
        )
        return reply

    async def do_chat(
        self,
        user_message: str,
        telegram_user_id: int,
        base64_image: str | None = None,
        mime_type: str | None = None,
    ) -> LlmReply:
        """Template method running up to MAX_TOOL_ITERATIONS turns of tool calling."""
        iteration = 0

        if is_rejection(user_message):
            search_query = self.chat_history_service.get_last_user_message(telegram_user_id)
        else:
            search_query = self.build_contextual_search_query(telegram_user_id, user_message)

        if not search_query or not search_query.strip():
            search_query = user_message

        rejected_faqs = self.chat_history_service.get_rejected_faq_questions(telegram_user_id)
        faq_context = await self.faq_embedding_service.build_faq_context(
            search_query or "", rejected_faqs
        )

        history = await self._get_conversation_history(telegram_user_id)
        conversation = self.build_initial_conversation(
            user_message,
            telegram_user_id,
            faq_context.text,
            base64_image,
            mime_type,
            history=history,
        )

        while iteration < self.MAX_TOOL_ITERATIONS:
            try:
                payload = await self.call_api(conversation, faq_context.text, telegram_user_id)
                await self.save_usage(payload, telegram_user_id)
                llm_response = self.parse_response(payload)

                if llm_response.has_tool_calls():
                    await self.run_tool_calls(conversation, llm_response, telegram_user_id)
                    iteration += 1
                    continue

                if llm_response.text:
                    if self._is_bare_promise(llm_response.text) and iteration + 1 < (
                        self.MAX_TOOL_ITERATIONS
                    ):
                        logger.info(
                            "%s promised to check something without calling a tool — nudging",
                            self.get_provider_name(),
                        )
                        self.add_retry_nudge_to_conversation(
                            conversation, llm_response.text, self.RETRY_NUDGE
                        )
                        iteration += 1
                        continue
                    return LlmReply(text=llm_response.text, faq_context=faq_context)

                raise LlmProcessingException(
                    "No content returned",
                    "Модель не вернула ответа. Попробуйте переформулировать вопрос.",
                )
            except LlmProcessingException:
                raise
            except Exception as e:
                logger.error("%s request failed: %s", self.get_provider_name(), e, exc_info=True)
                raise LlmProcessingException(
                    str(e),
                    "Произошла ошибка при обработке запроса. Попробуйте позже.",
                    cause=e,
                ) from e

        raise LlmProcessingException(
            "Max iterations reached",
            "Превышено количество попыток обработки запроса. Пожалуйста, попробуйте ещё раз.",
        )

    async def run_tool_calls(
        self,
        conversation: list[dict[str, Any]],
        llm_response: LlmResponse,
        telegram_user_id: int,
    ) -> None:
        """Execute tools requested by the model and append outputs to conversation."""
        logger.info(
            "%s requested %d tool call(s)",
            self.get_provider_name(),
            len(llm_response.tool_calls),
        )
        self.add_tool_calls_to_conversation(conversation, llm_response)

        for tc in llm_response.tool_calls:
            logger.info("Executing tool: %s with args: %s", tc.name, tc.arguments)
            tool_result = await self.mcp_router.call_tool(tc.name, tc.arguments, telegram_user_id)
            logger.info("Tool %s result: %s", tc.name, self._truncate(tool_result))
            self.add_tool_result_to_conversation(conversation, tc, tool_result)

    def _is_bare_promise(self, text: str) -> bool:
        """True when the model announced it would check something and did nothing.

        Sending this straight to the user dead-ends the conversation: they are
        told to wait for an answer that is never coming, because the turn is
        over. Only meaningful while tools are actually on the table.
        """
        stripped = text.strip()
        if not stripped or len(stripped) > self.MAX_BARE_PROMISE_LENGTH:
            return False
        if not self.mcp_router.list_tools():
            return False
        return self.BARE_PROMISE_PATTERN.search(stripped) is not None

    def add_retry_nudge_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        assistant_text: str,
        instruction: str,
    ) -> None:
        """Record the stalled reply and ask for action instead.

        Default is the {role, content} shape used by OpenAI and DeepSeek;
        Gemini overrides it with its own parts format.
        """
        if assistant_text:
            conversation.append({"role": "assistant", "content": assistant_text})
        conversation.append({"role": "user", "content": instruction})

    def build_contextual_search_query(
        self, telegram_user_id: int, user_message: str | None
    ) -> str | None:
        """Prefix the previous user message when this one cannot stand on its own."""
        if user_message is None or not user_message.strip():
            return user_message

        last_msg = self.chat_history_service.get_last_user_message(telegram_user_id)
        if (
            not last_msg
            or not last_msg.strip()
            or last_msg.strip().lower() == user_message.strip().lower()
        ):
            return user_message

        trimmed = user_message.strip()
        has_letters = any(c.isalpha() for c in trimmed)
        is_follow_up = (not has_letters) or bool(self.FOLLOW_UP_PATTERN.search(trimmed))

        return f"{last_msg} {trimmed}" if is_follow_up else trimmed

    async def _get_conversation_history(self, telegram_user_id: int) -> list[dict[str, Any]]:
        """Fetch chronological history formatted for provider."""
        return await self.chat_history_service.get_history(telegram_user_id)

    @classmethod
    def _truncate(cls, s: str | None) -> str | None:
        if s is not None and len(s) > cls.TOOL_RESULT_MAX_LOG_LENGTH:
            return s[: cls.TOOL_RESULT_MAX_LOG_LENGTH] + "..."
        return s

    def decode_json(self, response: httpx.Response) -> dict[str, Any]:
        """Turn one provider response into the dict every other step reads.

        The body used to be handed on as a string and parsed again by
        parse_response and once more by save_usage — three passes over the same
        payload on every turn, and up to five turns per answer.
        """
        try:
            payload = json.loads(response.text)
        except ValueError as e:
            logger.error("%s returned a body that is not JSON: %s", self.get_provider_name(), e)
            raise LlmProcessingException(
                f"{self.get_provider_name()} returned malformed JSON: {e}",
                "Ошибка обработки ответа модели.",
            ) from e

        if not isinstance(payload, dict):
            raise LlmProcessingException(
                f"{self.get_provider_name()} returned {type(payload).__name__}, expected an object",
                "Ошибка обработки ответа модели.",
            )
        return payload

    async def save_usage(self, payload: dict[str, Any], telegram_user_id: int) -> None:
        """Persist what the call cost. Providers only say where the numbers are."""
        if self.db_manager is None:
            return
        try:
            usage = self.extract_usage(payload)
            if usage is None:
                return
            async with self.db_manager.session() as session:
                session.add(
                    LlmTokenUsage(
                        telegram_id=telegram_user_id,
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                    )
                )
        except Exception as e:
            logger.warning("Failed to save token usage: %s", e)

    @abstractmethod
    def extract_usage(self, payload: dict[str, Any]) -> TokenUsage | None:
        """Read the provider's token counters, or None when it reported none."""
        ...

    @abstractmethod
    def build_initial_conversation(
        self,
        user_message: str,
        telegram_user_id: int,
        faq_context: str,
        base64_image: str | None,
        mime_type: str | None,
        history: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Build provider-specific initial conversation payload."""
        ...

    @abstractmethod
    async def call_api(
        self,
        conversation: list[dict[str, Any]],
        faq_context: str,
        telegram_user_id: int,
    ) -> dict[str, Any]:
        """Execute the HTTP request and return the decoded response body."""
        ...

    @abstractmethod
    def parse_response(self, payload: dict[str, Any]) -> LlmResponse:
        """Turn a decoded response body into a structured LlmResponse."""
        ...

    @abstractmethod
    def add_tool_calls_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        response: LlmResponse,
    ) -> None:
        """Add assistant tool calls to conversation history."""
        ...

    @abstractmethod
    def add_tool_result_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        tool_call: ToolCall,
        tool_result: str,
    ) -> None:
        """Add tool execution result to conversation history."""
        ...

    @abstractmethod
    def get_provider_name(self) -> str:
        """Return provider display name."""
        ...
