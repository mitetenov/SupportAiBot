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
    TokenUsage,
    ToolCall,
)
from app.retry import post_with_retry

if TYPE_CHECKING:
    from app.llm.mcp_router import McpRouter
    from app.rag.service import FaqEmbeddingService
    from app.storage.chat_history import ChatHistoryService
    from app.storage.database import DatabaseSessionManager

logger = logging.getLogger(__name__)

UNSUPPORTED_SCHEMA_FIELDS: set[str] = {"$schema", "additionalProperties", "propertyNames"}
SCHEMA_ARRAY_FIELDS: set[str] = {"anyOf", "oneOf", "allOf"}


def sanitize_schema_params(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Rewrite a JSON Schema from MCP server into subset Gemini accepts."""
    if not schema or not isinstance(schema, dict):
        return {}

    cleaned: dict[str, Any] = {}
    for field_name, value in schema.items():
        if field_name in UNSUPPORTED_SCHEMA_FIELDS:
            continue
        _copy_sanitized_field(cleaned, field_name, value)
    return cleaned


def _copy_sanitized_field(cleaned: dict[str, Any], field_name: str, value: Any) -> None:
    if field_name == "const":
        cleaned["enum"] = [value]
    elif field_name == "any_of":
        _copy_schema_array(cleaned, "anyOf", value)
    elif field_name == "properties":
        _copy_properties(cleaned, field_name, value)
    elif field_name == "items":
        _copy_nested_schema(cleaned, field_name, value)
    elif field_name in SCHEMA_ARRAY_FIELDS and isinstance(value, list):
        _copy_schema_array(cleaned, field_name, value)
    else:
        cleaned[field_name] = value


def _copy_schema_array(cleaned: dict[str, Any], target_field: str, value: Any) -> None:
    if isinstance(value, list):
        cleaned[target_field] = [
            sanitize_schema_params(item) if isinstance(item, dict) else item for item in value
        ]
    else:
        cleaned[target_field] = value


def _copy_properties(cleaned: dict[str, Any], field_name: str, value: Any) -> None:
    if not isinstance(value, dict):
        cleaned[field_name] = value
        return
    cleaned_props: dict[str, Any] = {}
    for prop_name, prop_schema in value.items():
        cleaned_props[prop_name] = (
            sanitize_schema_params(prop_schema) if isinstance(prop_schema, dict) else prop_schema
        )
    cleaned[field_name] = cleaned_props


def _copy_nested_schema(cleaned: dict[str, Any], field_name: str, value: Any) -> None:
    cleaned[field_name] = sanitize_schema_params(value) if isinstance(value, dict) else value


class GeminiClient(AbstractLlmClient):
    """Client for Google Gemini generateContent REST API."""

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
        self.model = settings.gemini_model or "gemini-2.5-flash"
        self.base_url = (
            settings.gemini_base_url or "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
        self.api_key = reveal(settings.gemini_api_key)
        self._http_client = http_client
        self._own_client = False
        self.sanitized_tools = self._build_sanitized_tools()

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
        return "Gemini"

    def _build_sanitized_tools(self) -> list[dict[str, Any]]:
        tools = self.mcp_router.list_tools()
        if not tools:
            return []
        sanitized: list[dict[str, Any]] = []
        for tool in tools:
            decl: dict[str, Any] = {"name": tool.name}
            if tool.description:
                decl["description"] = tool.description
            if tool.input_schema:
                decl["parameters"] = sanitize_schema_params(tool.input_schema)
            sanitized.append(decl)
        return sanitized

    def build_request_body(self, contents: list[dict[str, Any]]) -> dict[str, Any]:
        """Build generateContent request JSON body."""
        body: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": SupportPrompt.SYSTEM}]},
            "contents": contents,
        }
        if self.sanitized_tools:
            body["tools"] = [{"function_declarations": self.sanitized_tools}]

        body["tool_config"] = {"function_calling_config": {"mode": "AUTO"}}
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
        contents: list[dict[str, Any]] = []

        dynamic_context = f"Telegram ID: {telegram_user_id}"
        if faq_context and faq_context.strip():
            dynamic_context += f"\n\n{faq_context.strip()}"

        contents.append(
            {
                "role": "user",
                "parts": [
                    {"text": f"[Система: Контекст текущего пользователя]\n{dynamic_context}"}
                ],
            }
        )
        contents.append(
            {
                "role": "model",
                "parts": [{"text": "Принято. Я готов помочь пользователю."}],
            }
        )

        if history:
            contents.extend(history)

        user_parts: list[dict[str, Any]] = []
        if base64_image and base64_image.strip():
            if user_message and user_message.strip():
                user_parts.append({"text": user_message})
            else:
                user_parts.append({"text": user_message or ""})
            user_parts.append(
                {
                    "inline_data": {
                        "mime_type": mime_type if mime_type else "image/jpeg",
                        "data": base64_image,
                    }
                }
            )
        else:
            user_parts.append({"text": user_message})

        contents.append({"role": "user", "parts": user_parts})
        return contents

    async def _get_conversation_history(self, telegram_user_id: int) -> list[dict[str, Any]]:
        return await self.chat_history_service.to_gemini_contents(telegram_user_id)

    async def call_api(
        self,
        conversation: list[dict[str, Any]],
        faq_context: str,
        telegram_user_id: int,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/models/{self.model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        body = self.build_request_body(conversation)
        logger.debug("Gemini request (%d tools available)", len(self.sanitized_tools))

        response = await post_with_retry(
            self.http_client,
            url,
            headers=headers,
            json=body,
            description="Gemini API",
        )
        if response.status_code >= 400:
            logger.error("Gemini API error (%d): %s", response.status_code, response.text)
            raise LlmProcessingException(
                f"Gemini API error: {response.status_code} - {response.text}",
                "Произошла ошибка при обработке запроса. Попробуйте позже.",
            )
        return self.decode_json(response)

    def parse_response(self, payload: dict[str, Any]) -> LlmResponse:
        try:
            json_response = payload
            candidates = json_response.get("candidates")
            if not candidates or not isinstance(candidates, list) or len(candidates) == 0:
                block_reason = (
                    json.dumps(json_response.get("promptFeedback"))
                    if "promptFeedback" in json_response
                    else "неизвестно"
                )
                logger.error("Empty candidates in Gemini response. Block: %s", block_reason)
                raise LlmProcessingException(
                    "Empty candidates",
                    "Не удалось получить ответ от модели. Возможно, запрос был заблокирован фильтрами.",
                )

            content = candidates[0].get("content")
            if not content:
                raise LlmProcessingException(
                    "No content in candidate",
                    "Модель не вернула ответа. Попробуйте переформулировать вопрос.",
                )

            parts = content.get("parts")
            if not parts or not isinstance(parts, list):
                raise LlmProcessingException(
                    "Empty parts",
                    "Модель не вернула ответа. Попробуйте переформулировать вопрос.",
                )

            text_builder: list[str] = []
            function_calls: list[ToolCall] = []
            raw_parts: list[dict[str, Any]] = []

            for part in parts:
                raw_parts.append(part)
                if "text" in part and part["text"] is not None:
                    text_builder.append(part["text"])
                if "functionCall" in part:
                    fc = part["functionCall"]
                    fn_name = fc.get("name", "")
                    fn_args = fc.get("args") or {}
                    thought_sig = fc.get("thought_signature")
                    function_calls.append(
                        ToolCall(
                            name=fn_name,
                            id="",
                            arguments=fn_args,
                            thought_signature=thought_sig,
                        )
                    )

            return LlmResponse(
                text="".join(text_builder),
                tool_calls=function_calls,
                raw_parts=raw_parts,
            )
        except LlmProcessingException:
            raise
        except Exception as e:
            logger.error("Failed to parse Gemini response: %s", e)
            raise LlmProcessingException("Parse error", "Ошибка обработки ответа модели.") from e

    def add_tool_calls_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        response: LlmResponse,
    ) -> None:
        model_parts: list[dict[str, Any]] = []
        if response.raw_parts:
            model_parts.extend(response.raw_parts)
        else:
            if response.text:
                model_parts.append({"text": response.text})
            for tc in response.tool_calls:
                fc: dict[str, Any] = {"name": tc.name, "args": tc.arguments}
                if tc.thought_signature is not None:
                    fc["thought_signature"] = tc.thought_signature
                model_parts.append({"functionCall": fc})

        conversation.append({"role": "model", "parts": model_parts})

    def add_tool_result_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        tool_call: ToolCall,
        tool_result: str,
    ) -> None:
        try:
            response_content = json.loads(tool_result)
        except Exception:
            response_content = {"output": tool_result}

        function_response: dict[str, Any] = {
            "name": tool_call.name,
            "response": response_content,
        }
        if tool_call.thought_signature is not None:
            function_response["thought_signature"] = tool_call.thought_signature

        conversation.append(
            {
                "role": "function",
                "parts": [{"functionResponse": function_response}],
            }
        )

    def add_retry_nudge_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        assistant_text: str,
        instruction: str,
    ) -> None:
        if assistant_text:
            conversation.append({"role": "model", "parts": [{"text": assistant_text}]})
        conversation.append({"role": "user", "parts": [{"text": instruction}]})

    def extract_usage(self, payload: dict[str, Any]) -> TokenUsage | None:
        usage = payload.get("usageMetadata")
        if not isinstance(usage, dict):
            return None
        return TokenUsage(
            prompt_tokens=int(usage.get("promptTokenCount") or 0),
            completion_tokens=int(usage.get("candidatesTokenCount") or 0),
            total_tokens=int(usage.get("totalTokenCount") or 0),
        )
