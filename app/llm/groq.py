from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import httpx

from app.config import Settings, reveal
from app.constants import SupportPrompt
from app.llm.base import (
    AbstractLlmClient,
    LlmProcessingException,
    LlmReply,
    LlmResponse,
    TokenUsage,
    ToolCall,
    is_balance_exhaustion_message,
)
from app.retry import post_with_retry

if TYPE_CHECKING:
    from app.llm.mcp_router import McpRouter
    from app.rag.service import FaqEmbeddingService
    from app.storage.chat_history import ChatHistoryService
    from app.storage.database import DatabaseSessionManager

logger = logging.getLogger(__name__)

DEEPSEEK_REASONING_EFFORT_MAP: dict[str, str] = {
    "minimal": "high",
    "low": "high",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}


def supports_reasoning(model: str) -> bool:
    """Return whether the official Groq model supports dual thinking mode."""
    normalized = model.strip().lower()
    return normalized.startswith("deepseek-v4-") or normalized in {
        "deepseek-chat",
        "deepseek-reasoner",
    }


class GroqClient(AbstractLlmClient):
    """Client for Groq API using OpenAI-compatible /chat/completions."""

    TEMPERATURE: float = 0.3

    def __init__(
        self,
        settings: Settings,
        mcp_router: McpRouter,
        chat_history_service: ChatHistoryService,
        faq_embedding_service: FaqEmbeddingService,
        db_manager: DatabaseSessionManager | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_embedding_service=faq_embedding_service,
            db_manager=db_manager,
        )
        self.settings = settings
        self.model = settings.groq_model or "llama-3.3-70b-versatile"
        self.base_url = (settings.groq_base_url or "https://api.groq.com/openai/v1").rstrip("/")
        self.api_key = reveal(settings.groq_api_key)
        self.reasoning_effort = settings.reasoning_effort
        self.reasoning_supported = False
        self._http_client = http_client
        self._own_client = False
        self.tool_definitions = self._build_tool_definitions()
        self._log_reasoning_configuration()

    def _log_reasoning_configuration(self) -> None:
        if not self.reasoning_supported:
            if self.reasoning_effort != "none":
                logger.warning(
                    "Groq model %s does not support reasoning; "
                    "REASONING_EFFORT=%s is ignored and requests are sent without reasoning",
                    self.model,
                    self.reasoning_effort,
                )
            return
        native_effort = (
            "none"
            if self.reasoning_effort == "none"
            else DEEPSEEK_REASONING_EFFORT_MAP[self.reasoning_effort]
        )
        logger.info(
            "Groq reasoning %s for model %s "
            "(REASONING_EFFORT=%s, native effort=%s, MCP tools compatible)",
            "disabled" if self.reasoning_effort == "none" else "enabled",
            self.model,
            self.reasoning_effort,
            native_effort,
        )

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
            self._own_client = True
        return self._http_client

    async def close(self) -> None:
        if self._own_client and self._http_client is not None:
            await self._http_client.aclose()

    def supports_images(self) -> bool:
        return False

    def get_provider_name(self) -> str:
        return "Groq"

    def _build_tool_definitions(self) -> list[dict[str, Any]]:
        tools = self.mcp_router.list_tools()
        definitions: list[dict[str, Any]] = []
        for tool in tools:
            params = (
                tool.input_schema if tool.input_schema else {"type": "object", "properties": {}}
            )
            function: dict[str, Any] = {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": params,
            }
            definitions.append({"type": "function", "function": function})
        return definitions

    def build_request_body(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Build chat completions request JSON body."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        thinking_enabled = self.reasoning_supported and self.reasoning_effort != "none"
        if self.reasoning_supported:
            body["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
            if thinking_enabled:
                body["reasoning_effort"] = DEEPSEEK_REASONING_EFFORT_MAP[self.reasoning_effort]
        if not thinking_enabled:
            body["temperature"] = self.TEMPERATURE
        if self.tool_definitions:
            body["tools"] = self.tool_definitions
            # Groq V4 rejects tool_choice when thinking mode is enabled.
            if not thinking_enabled:
                body["tool_choice"] = "auto"
        return body

    def build_initial_conversation(
        self,
        user_message: str,
        telegram_user_id: int,
        faq_context: str | None = None,
        base64_image: str | None = None,
        mime_type: str | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        messages.append({"role": "system", "content": SupportPrompt.SYSTEM})

        dynamic_context = SupportPrompt.dynamic_context(faq_context, telegram_user_id)
        messages.append({"role": "system", "content": dynamic_context})

        if history:
            messages.extend(history)

        messages.append({"role": "user", "content": user_message})
        return messages

    async def chat_with_image(
        self,
        user_message: str,
        telegram_user_id: int,
        base64_image: str,
        mime_type: str | None = None,
    ) -> LlmReply:
        raise LlmProcessingException(
            "Image not supported",
            "Groq не поддерживает обработку изображений. Переключите провайдера на Gemini (LLM_PROVIDER=gemini) или опишите проблему текстом.",
        )

    async def call_api(
        self,
        conversation: list[dict[str, Any]],
        faq_context: str,
        telegram_user_id: int,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = self.build_request_body(conversation)
        logger.debug("Groq request (%d tools available)", len(self.tool_definitions))

        response = await post_with_retry(
            self.http_client,
            url,
            headers=headers,
            json=body,
            description="Groq API",
        )
        if response.status_code >= 400:
            logger.error("Groq API error (model=%s, status=%d)", self.model, response.status_code)
            raise LlmProcessingException(
                f"Groq API error (model={self.model}, status={response.status_code})",
                "Произошла ошибка при обработке запроса. Попробуйте позже.",
                status_code=response.status_code,
                fallback_eligible=is_balance_exhaustion_message(response.text),
            )
        return self.decode_json(response)

    def parse_response(self, payload: dict[str, Any]) -> LlmResponse:
        try:
            choices = payload.get("choices")
            if not choices or not isinstance(choices, list) or len(choices) == 0:
                logger.error("Empty choices in Groq response: %s", payload)
                raise LlmProcessingException(
                    "Empty choices",
                    "Не удалось получить ответ от модели. Попробуйте позже.",
                )

            message = choices[0].get("message")
            if not message:
                raise LlmProcessingException(
                    "No message in response",
                    "Не удалось получить ответ от модели. Попробуйте позже.",
                )

            content = message.get("content") or ""
            tool_calls: list[ToolCall] = []
            tool_calls_node = message.get("tool_calls")
            if tool_calls_node and isinstance(tool_calls_node, list):
                for tc in tool_calls_node:
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    fn_args_str = fn.get("arguments", "{}")
                    if isinstance(fn_args_str, str):
                        try:
                            args = json.loads(fn_args_str) if fn_args_str else {}
                        except Exception:
                            args = {}
                    else:
                        args = fn_args_str or {}
                    tc_id = tc.get("id", "")
                    tool_calls.append(ToolCall(name=fn_name, id=tc_id, arguments=args))

            reasoning_content = message.get("reasoning_content")
            return LlmResponse(
                text=content,
                tool_calls=tool_calls,
                reasoning_content=(
                    reasoning_content if isinstance(reasoning_content, str) else None
                ),
            )
        except LlmProcessingException:
            raise
        except Exception as e:
            logger.error("Failed to parse Groq response: %s", e)
            raise LlmProcessingException("Parse error", "Ошибка обработки ответа модели.") from e

    def add_tool_calls_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        response: LlmResponse,
    ) -> None:
        tool_call_maps: list[dict[str, Any]] = []
        for tc in response.tool_calls:
            tool_call_maps.append(
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments or {}),
                    },
                }
            )
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            # Groq thinking tool loops require non-null assistant content.
            "content": response.text,
            "tool_calls": tool_call_maps,
        }
        if response.reasoning_content is not None:
            assistant_message["reasoning_content"] = response.reasoning_content
        conversation.append(assistant_message)

    def add_tool_result_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        tool_call: ToolCall,
        tool_result: str,
    ) -> None:
        conversation.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": tool_result,
            }
        )

    def extract_usage(self, payload: dict[str, Any]) -> TokenUsage | None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        return TokenUsage(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        )
