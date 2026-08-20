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


class OpenAiClient(AbstractLlmClient):
    """Client for OpenAI Responses API (/responses) with tools and vision."""

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
        api_key = reveal(settings.openai_api_key).strip()
        if not api_key:
            raise ValueError("OpenAI API key must not be null or blank")

        self.settings = settings
        self.api_key = api_key
        self.model = settings.openai_model or "gpt-5.6-luna"
        self.base_url = (settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")
        self.temperature = settings.openai_temperature
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
        return True

    def get_provider_name(self) -> str:
        return "OpenAI"

    def _build_tool_definitions(self) -> list[dict[str, Any]]:
        tools = self.mcp_router.list_tools()
        definitions: list[dict[str, Any]] = []
        for tool in tools:
            params = (
                tool.input_schema if tool.input_schema else {"type": "object", "properties": {}}
            )
            function: dict[str, Any] = {
                "type": "function",
                "name": tool.name,
                "description": tool.description or "",
                "parameters": params,
            }
            definitions.append(function)
        return definitions

    def build_request_body(self, conversation: list[dict[str, Any]]) -> dict[str, Any]:
        """Build Responses API request JSON body."""
        body: dict[str, Any] = {
            "model": self.model,
            "input": conversation,
        }
        if self.tool_definitions:
            body["tools"] = self.tool_definitions
            body["tool_choice"] = "auto"
            body["reasoning"] = {"effort": "none"}

        if self.temperature is not None:
            body["temperature"] = self.temperature

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

        if base64_image and base64_image.strip():
            parts: list[dict[str, Any]] = []
            if user_message and user_message.strip():
                parts.append({"type": "input_text", "text": user_message})
            data_uri = f"data:{mime_type if mime_type else 'image/jpeg'};base64,{base64_image}"
            parts.append({"type": "input_image", "image_url": data_uri})
            messages.append({"role": "user", "content": parts})
        else:
            messages.append({"role": "user", "content": user_message})

        return messages

    async def call_api(
        self,
        conversation: list[dict[str, Any]],
        faq_context: str,
        telegram_user_id: int,
    ) -> str:
        url = f"{self.base_url}/responses"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = self.build_request_body(conversation)
        logger.debug(
            "OpenAI Responses API request (%d tools available)", len(self.tool_definitions)
        )

        response = await self.http_client.post(url, json=body, headers=headers)
        if response.status_code == 401:
            err_msg = (
                f"OpenAI API error (model={self.model}): 401 - {response.text} | "
                "Проверьте OPENAI_API_KEY и OPENAI_MODEL в .env"
            )
            logger.error(err_msg)
            raise LlmProcessingException(
                err_msg,
                "Произошла ошибка при обработке запроса. Попробуйте позже.",
            )

        if response.status_code >= 400:
            logger.error("OpenAI API error (%d): %s", response.status_code, response.text)
            raise LlmProcessingException(
                f"OpenAI API error (model={self.model}): {response.status_code} - {response.text}",
                "Произошла ошибка при обработке запроса. Попробуйте позже.",
            )

        return response.text

    def parse_response(self, raw_response: str) -> LlmResponse:
        try:
            json_response = json.loads(raw_response)
            output = json_response.get("output")
            if not output or not isinstance(output, list):
                logger.error("No output array in OpenAI Responses API response: %s", raw_response)
                raise LlmProcessingException(
                    "Empty output",
                    "Не удалось получить ответ от модели. Попробуйте позже.",
                )

            text_builder: list[str] = []
            tool_calls: list[ToolCall] = []

            for item in output:
                item_type = item.get("type", "")
                if item_type == "function_call":
                    fn_name = item.get("name", "")
                    call_id = item.get("call_id", "")
                    fn_args_str = item.get("arguments", "{}")
                    if isinstance(fn_args_str, str):
                        try:
                            args = json.loads(fn_args_str) if fn_args_str else {}
                        except Exception:
                            args = {}
                    else:
                        args = fn_args_str or {}
                    tool_calls.append(ToolCall(name=fn_name, id=call_id, arguments=args))
                elif item_type == "message":
                    content = item.get("content", [])
                    if isinstance(content, list):
                        for part in content:
                            if part.get("type") == "output_text" and part.get("text"):
                                text_builder.append(part["text"])

            full_text = "".join(text_builder)
            if not full_text and not tool_calls:
                logger.warning("No text or tool calls in OpenAI response: %s", raw_response)
                raise LlmProcessingException(
                    "Empty response",
                    "Модель не вернула ответа. Попробуйте переформулировать вопрос.",
                )

            return LlmResponse(text=full_text, tool_calls=tool_calls)
        except LlmProcessingException:
            raise
        except Exception as e:
            logger.error("Failed to parse OpenAI response: %s", e)
            raise LlmProcessingException(
                f"Parse error: {e}", "Ошибка обработки ответа модели."
            ) from e

    def add_tool_calls_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        response: LlmResponse,
    ) -> None:
        for tc in response.tool_calls:
            conversation.append(
                {
                    "type": "function_call",
                    "call_id": tc.id,
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments or {}),
                }
            )

    def add_tool_result_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        tool_call: ToolCall,
        tool_result: str,
    ) -> None:
        conversation.append(
            {
                "type": "function_call_output",
                "call_id": tool_call.id,
                "output": tool_result,
            }
        )

    async def save_usage(self, raw_response: str, telegram_user_id: int) -> None:
        if self.db_manager is None:
            return
        try:
            json_response = json.loads(raw_response)
            usage = json_response.get("usage")
            if usage:
                prompt_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
                completion_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
                total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
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
