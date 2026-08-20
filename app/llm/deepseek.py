from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import httpx

from app.config import Settings
from app.constants import SupportPrompt
from app.llm.base import (
    AbstractLlmClient,
    LlmProcessingException,
    LlmReply,
    LlmResponse,
    ToolCall,
)
from app.storage.models import LlmTokenUsage

if TYPE_CHECKING:
    from app.llm.mcp_router import McpRouter
    from app.rag.service import FaqEmbeddingService
    from app.storage.chat_history import ChatHistoryService
    from app.storage.database import DatabaseSessionManager

logger = logging.getLogger(__name__)


class DeepSeekClient(AbstractLlmClient):
    """Client for DeepSeek API using OpenAI-compatible /chat/completions."""

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
        self.model = settings.deepseek_model or "deepseek-chat"
        self.base_url = (settings.deepseek_base_url or "https://api.deepseek.com/v1").rstrip("/")
        self.api_key = settings.deepseek_api_key or ""
        self._http_client = http_client
        self._own_client = False
        self.tool_definitions = self._build_tool_definitions()

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
        return "DeepSeek"

    def _build_tool_definitions(self) -> list[dict[str, Any]]:
        tools = self.mcp_router.list_tools()
        definitions: list[dict[str, Any]] = []
        for tool in tools:
            params = tool.input_schema if tool.input_schema else {"type": "object", "properties": {}}
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
            "temperature": self.TEMPERATURE,
        }
        if self.tool_definitions:
            body["tools"] = self.tool_definitions
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

        dynamic_context = f"Telegram ID: {telegram_user_id}"
        if faq_context and faq_context.strip():
            dynamic_context += f"\n\n{faq_context.strip()}"
        messages.append({"role": "system", "content": dynamic_context})

        if history:
            messages.extend(history)

        messages.append({"role": "user", "content": user_message})
        return messages

    async def _get_conversation_history(self, telegram_user_id: int) -> list[dict[str, Any]]:
        return await self.chat_history_service.get_history(telegram_user_id)

    async def chat_with_image(
        self,
        user_message: str,
        telegram_user_id: int,
        base64_image: str,
        mime_type: str | None = None,
    ) -> LlmReply:
        raise LlmProcessingException(
            "Image not supported",
            "DeepSeek не поддерживает обработку изображений. Переключите провайдера на Gemini (LLM_PROVIDER=gemini) или опишите проблему текстом.",
        )

    async def call_api(
        self,
        conversation: list[dict[str, Any]],
        faq_context: str,
        telegram_user_id: int,
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = self.build_request_body(conversation)
        logger.debug("DeepSeek request (%d tools available)", len(self.tool_definitions))

        response = await self.http_client.post(url, json=body, headers=headers)
        if response.status_code >= 400:
            logger.error("DeepSeek API error (%d): %s", response.status_code, response.text)
            raise LlmProcessingException(
                f"DeepSeek API error: {response.status_code} - {response.text}",
                "Произошла ошибка при обработке запроса. Попробуйте позже.",
            )
        return response.text

    def parse_response(self, raw_response: str) -> LlmResponse:
        try:
            json_response = json.loads(raw_response)
            choices = json_response.get("choices")
            if not choices or not isinstance(choices, list) or len(choices) == 0:
                logger.error("Empty choices in DeepSeek response: %s", raw_response)
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

            return LlmResponse(text=content, tool_calls=tool_calls)
        except LlmProcessingException:
            raise
        except Exception as e:
            logger.error("Failed to parse DeepSeek response: %s", e)
            raise LlmProcessingException("Parse error", "Ошибка обработки ответа модели.") from e

    def add_tool_calls_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        response: LlmResponse,
    ) -> None:
        tool_call_maps: list[dict[str, Any]] = []
        for tc in response.tool_calls:
            tool_call_maps.append({
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments or {}),
                },
            })
        conversation.append({"role": "assistant", "tool_calls": tool_call_maps})

    def add_tool_result_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        tool_call: ToolCall,
        tool_result: str,
    ) -> None:
        conversation.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": tool_result,
        })

    async def save_usage(self, raw_response: str, telegram_user_id: int) -> None:
        if self.db_manager is None:
            return
        try:
            json_response = json.loads(raw_response)
            usage = json_response.get("usage")
            if usage:
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", 0)
                async with self.db_manager.session() as session:
                    record = LlmTokenUsage(
                        telegram_id=telegram_user_id,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                    )
                    session.add(record)
        except Exception as e:
            logger.warning("Failed to save token usage: %s", e)
