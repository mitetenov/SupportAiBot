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
    is_balance_exhaustion_message,
)
from app.logging_config import TRACE
from app.logging_http import create_logging_hooks
from app.logging_redaction import safe_serialize
from app.retry import post_with_retry

if TYPE_CHECKING:
    from app.llm.mcp_router import McpRouter
    from app.rag.service import FaqEmbeddingService
    from app.storage.chat_history import ChatHistoryService
    from app.storage.database import DatabaseSessionManager

logger = logging.getLogger(__name__)

OPENAI_GPT_56_EFFORTS: frozenset[str] = frozenset({"none", "low", "medium", "high", "xhigh", "max"})
OPENAI_GPT_55_EFFORTS: frozenset[str] = frozenset({"none", "low", "medium", "high", "xhigh"})
OPENAI_GPT_55_PRO_EFFORTS: frozenset[str] = frozenset({"medium", "high", "xhigh"})
OPENAI_GPT_5_EFFORTS: frozenset[str] = frozenset({"minimal", "low", "medium", "high"})
OPENAI_GPT_5_PRO_EFFORTS: frozenset[str] = frozenset({"high"})
OPENAI_COMMON_REASONING_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high"})


def supported_reasoning_efforts(model: str) -> frozenset[str] | None:
    """Return the documented effort set for a known OpenAI model family."""
    normalized = model.strip().lower()
    if normalized.startswith("gpt-5.6"):
        return OPENAI_GPT_56_EFFORTS
    if normalized.startswith("gpt-5.5-pro"):
        return OPENAI_GPT_55_PRO_EFFORTS
    if normalized.startswith("gpt-5.5"):
        return OPENAI_GPT_55_EFFORTS
    if normalized.startswith("gpt-5-pro"):
        return OPENAI_GPT_5_PRO_EFFORTS
    if normalized == "gpt-5" or normalized.startswith(("gpt-5-", "gpt-5-mini", "gpt-5-nano")):
        return OPENAI_GPT_5_EFFORTS
    if normalized.startswith(("gpt-5.1", "gpt-5.2", "gpt-5.3", "gpt-5.4")):
        return OPENAI_GPT_55_EFFORTS
    if normalized.startswith(("o1", "o3", "o4")):
        return OPENAI_COMMON_REASONING_EFFORTS
    return None


def supports_reasoning(model: str) -> bool:
    """Return whether an OpenAI model family accepts Responses reasoning config."""
    return supported_reasoning_efforts(model) is not None


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
        self.reasoning_effort = settings.reasoning_effort
        self.reasoning_supported = supports_reasoning(self.model)
        allowed_efforts = supported_reasoning_efforts(self.model)
        if allowed_efforts is not None and self.reasoning_effort not in allowed_efforts:
            raise ValueError(
                f"OpenAI model {self.model} не поддерживает REASONING_EFFORT={self.reasoning_effort}. "
                f"Допустимые значения: {', '.join(sorted(allowed_efforts))}"
            )
        self._http_client = http_client
        self._own_client = False
        self.tool_definitions = self._build_tool_definitions()
        self._log_reasoning_configuration()

    def _log_reasoning_configuration(self) -> None:
        if not self.reasoning_supported:
            if self.reasoning_effort != "none":
                logger.warning(
                    "OpenAI model %s does not support reasoning; "
                    "REASONING_EFFORT=%s is ignored and requests are sent without reasoning",
                    self.model,
                    self.reasoning_effort,
                )
            return
        logger.info(
            "OpenAI reasoning %s for model %s (REASONING_EFFORT=%s, MCP tools compatible)",
            "disabled" if self.reasoning_effort == "none" else "enabled",
            self.model,
            self.reasoning_effort,
        )
        if self.reasoning_effort != "none" and self.temperature is not None:
            logger.warning(
                "OPENAI_TEMPERATURE=%s is ignored while REASONING_EFFORT=%s is enabled",
                self.temperature,
                self.reasoning_effort,
            )

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0), event_hooks=create_logging_hooks()
            )
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

        if self.reasoning_supported:
            body["reasoning"] = {"effort": self.reasoning_effort}

        if self.temperature is not None and (
            not self.reasoning_supported or self.reasoning_effort == "none"
        ):
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

        dynamic_context = SupportPrompt.dynamic_context(faq_context, telegram_user_id)
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
    ) -> dict[str, Any]:
        url = f"{self.base_url}/responses"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = self.build_request_body(conversation)
        if logger.isEnabledFor(TRACE):
            effective_effort = self.reasoning_effort if self.reasoning_supported else "none"
            logger.log(
                TRACE,
                "OpenAI Responses API request (model=%s, configured_effort=%s, effective_effort=%s): %s",
                self.model,
                self.reasoning_effort,
                effective_effort,
                safe_serialize(body),
            )
        logger.debug(
            "OpenAI Responses API request (%d tools available)", len(self.tool_definitions)
        )

        response = await post_with_retry(
            self.http_client,
            url,
            headers=headers,
            json=body,
            description="OpenAI Responses API",
        )
        if response.status_code == 401:
            err_msg = f"OpenAI API error (model={self.model}, status=401)"
            logger.error(err_msg)
            raise LlmProcessingException(
                err_msg,
                "Произошла ошибка при обработке запроса. Попробуйте позже.",
                status_code=response.status_code,
            )

        if response.status_code >= 400:
            logger.error("OpenAI API error (model=%s, status=%d)", self.model, response.status_code)
            raise LlmProcessingException(
                f"OpenAI API error (model={self.model}, status={response.status_code})",
                "Произошла ошибка при обработке запроса. Попробуйте позже.",
                status_code=response.status_code,
                fallback_eligible=is_balance_exhaustion_message(response.text),
            )

        return self.decode_json(response)

    def parse_response(self, payload: dict[str, Any]) -> LlmResponse:
        try:
            output = payload.get("output")
            if not output or not isinstance(output, list):
                logger.error("No output array in OpenAI Responses API response: %s", payload)
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
                logger.warning("No text or tool calls in OpenAI response: %s", payload)
                raise LlmProcessingException(
                    "Empty response",
                    "Модель не вернула ответа. Попробуйте переформулировать вопрос.",
                )

            resp = LlmResponse(
                text=full_text,
                tool_calls=tool_calls,
                raw_parts=[item for item in output if isinstance(item, dict)],
            )
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "OpenAI Responses API parsed response (model=%s): text=%s tool_calls=%s",
                    self.model,
                    full_text,
                    [
                        {"name": tc.name, "id": tc.id, "arguments": tc.arguments}
                        for tc in tool_calls
                    ],
                )
            return resp
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
        if response.raw_parts:
            conversation.extend(response.raw_parts)
            return
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

    def extract_usage(self, payload: dict[str, Any]) -> TokenUsage | None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=int(usage.get("total_tokens") or prompt_tokens + completion_tokens),
        )
