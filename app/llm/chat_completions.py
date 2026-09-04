from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

import httpx

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
from app.llm.fallback import _FALLBACK_STATUS_CODES
from app.logging_config import TRACE, log_failure
from app.logging_http import create_logging_hooks
from app.logging_redaction import safe_serialize
from app.retry import post_with_retry

if TYPE_CHECKING:
    from app.llm.mcp_router import McpRouter
    from app.rag.service import FaqEmbeddingService
    from app.storage.chat_history import ChatHistoryService
    from app.storage.database import DatabaseSessionManager

logger = logging.getLogger(__name__)

_RAW_REASONING = re.compile(r"<think\b[^>]*>.*?(?:</think\s*>|$)", re.DOTALL | re.IGNORECASE)


class ChatCompletionsClient(AbstractLlmClient):
    """Shared base client for text Chat Completions providers (OpenRouter, Z.AI, etc.)."""

    def __init__(
        self,
        mcp_router: McpRouter,
        chat_history_service: ChatHistoryService,
        faq_embedding_service: FaqEmbeddingService,
        db_manager: DatabaseSessionManager | None = None,
        http_client: httpx.AsyncClient | None = None,
        *,
        model: str,
        base_url: str,
        api_key: str,
        request_timeout_seconds: float = 120.0,
    ) -> None:
        super().__init__(
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_embedding_service=faq_embedding_service,
            db_manager=db_manager,
        )
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.request_timeout_seconds = request_timeout_seconds
        self._http_client = http_client
        self._own_client = False
        self.tool_definitions = self._build_tool_definitions()

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.request_timeout_seconds),
                event_hooks=create_logging_hooks(),
            )
            self._own_client = True
        return self._http_client

    async def close(self) -> None:
        if self._own_client and self._http_client is not None:
            await self._http_client.aclose()

    def supports_images(self) -> bool:
        return False

    async def chat_with_image(
        self,
        user_message: str,
        telegram_user_id: int,
        base64_image: str,
        mime_type: str | None = None,
    ) -> LlmReply:
        raise LlmProcessingException(
            "Image not supported",
            f"{self.get_provider_name()} не поддерживает обработку изображений. Опишите проблему текстом.",
        )

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

    def build_request_body(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Build standard OpenAI-compatible chat completions request JSON body."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if self.tool_definitions:
            body["tools"] = self.tool_definitions
            body["tool_choice"] = "auto"
        return body

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

        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE,
                "%s API request (model=%s): %s",
                self.get_provider_name(),
                self.model,
                safe_serialize(body),
            )
            logger.log(
                TRACE,
                "%s request (%d tools available)",
                self.get_provider_name(),
                len(self.tool_definitions),
            )

        try:
            response = await post_with_retry(
                self.http_client,
                url,
                headers=headers,
                json=body,
                timeout=self.request_timeout_seconds,
                description=f"{self.get_provider_name()} API",
            )
        except asyncio.CancelledError:
            raise

        if response.status_code >= 400:
            logger.error(
                "%s API error (model=%s, status=%d)",
                self.get_provider_name(),
                self.model,
                response.status_code,
            )
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "%s API error details: status=%d, body=%s",
                    self.get_provider_name(),
                    response.status_code,
                    response.text,
                )
            payload: dict[str, Any] | None = None
            try:
                parsed = json.loads(response.text)
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = None
            self.check_response_error(response, payload)
            raise LlmProcessingException(
                f"{self.get_provider_name()} API error (model={self.model}, status={response.status_code})",
                "Произошла ошибка при обработке запроса. Попробуйте позже.",
                status_code=response.status_code,
            )

        payload = self.decode_json(response)
        self.check_response_error(response, payload)
        return payload

    def check_response_error(
        self, response: httpx.Response, payload: dict[str, Any] | None
    ) -> None:
        """Inspect HTTP response and parsed payload for errors.

        Raises LlmProcessingException with safe metadata (no leaked payloads or keys).
        """
        if response.status_code >= 400:
            raw_msg = ""
            if payload and isinstance(payload.get("error"), (dict, str)):
                err = payload["error"]
                raw_msg = str(err.get("message") if isinstance(err, dict) else err)
            elif response.text:
                raw_msg = response.text

            fallback_eligible = (
                response.status_code in _FALLBACK_STATUS_CODES
            ) or is_balance_exhaustion_message(raw_msg)
            raise LlmProcessingException(
                f"{self.get_provider_name()} API error (model={self.model}, status={response.status_code})",
                "Произошла ошибка при обработке запроса. Попробуйте позже.",
                status_code=response.status_code,
                fallback_eligible=fallback_eligible,
            )

        if payload is not None and "error" in payload and payload["error"] is not None:
            err = payload["error"]
            raw_msg = str(err.get("message") if isinstance(err, dict) else err)
            fallback_eligible = is_balance_exhaustion_message(raw_msg)
            raise LlmProcessingException(
                f"{self.get_provider_name()} API error (model={self.model})",
                "Произошла ошибка при обработке запроса. Попробуйте позже.",
                fallback_eligible=fallback_eligible,
            )

    def parse_response(self, payload: dict[str, Any]) -> LlmResponse:
        choices = payload.get("choices")
        if not choices or not isinstance(choices, list) or len(choices) == 0:
            log_failure(
                logger,
                f"{self.get_provider_name()} response has no choices",
                provider=self.get_provider_name(),
            )
            raise LlmProcessingException(
                f"{self.get_provider_name()} response has no choices",
                "Не удалось получить ответ от модели. Попробуйте позже.",
            )

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise LlmProcessingException(
                f"{self.get_provider_name()} choice is not an object",
                "Не удалось получить ответ от модели. Попробуйте позже.",
            )

        message = first_choice.get("message")
        if not message or not isinstance(message, dict):
            raise LlmProcessingException(
                f"{self.get_provider_name()} choice has no valid message",
                "Не удалось получить ответ от модели. Попробуйте позже.",
            )

        raw_content = message.get("content")
        if raw_content is not None:
            if not isinstance(raw_content, str):
                raise LlmProcessingException(
                    f"{self.get_provider_name()} message content must be string or null",
                    "Ошибка обработки ответа модели.",
                )
            content = _RAW_REASONING.sub("", raw_content).strip()
        else:
            content = ""

        tool_calls: list[ToolCall] = []
        tool_calls_node = message.get("tool_calls")
        if tool_calls_node is not None:
            if not isinstance(tool_calls_node, list):
                raise LlmProcessingException(
                    f"{self.get_provider_name()} tool_calls must be a list",
                    "Ошибка обработки ответа модели.",
                )
            seen_ids: set[str] = set()
            for tc in tool_calls_node:
                if not isinstance(tc, dict):
                    raise LlmProcessingException(
                        f"{self.get_provider_name()} tool call entry must be an object",
                        "Ошибка обработки ответа модели.",
                    )
                tc_id = tc.get("id")
                if not tc_id or not isinstance(tc_id, str) or not tc_id.strip():
                    raise LlmProcessingException(
                        f"{self.get_provider_name()} tool call missing or empty id",
                        "Ошибка обработки ответа модели.",
                    )
                if tc_id in seen_ids:
                    raise LlmProcessingException(
                        f"Duplicate tool call id: {tc_id}",
                        "Ошибка обработки ответа модели.",
                    )
                seen_ids.add(tc_id)

                fn = tc.get("function")
                if not fn or not isinstance(fn, dict):
                    raise LlmProcessingException(
                        f"{self.get_provider_name()} tool call missing function object",
                        "Ошибка обработки ответа модели.",
                    )
                fn_name = fn.get("name")
                if not fn_name or not isinstance(fn_name, str) or not fn_name.strip():
                    raise LlmProcessingException(
                        f"{self.get_provider_name()} tool call missing function name",
                        "Ошибка обработки ответа модели.",
                    )

                if "arguments" not in fn or fn.get("arguments") is None:
                    raise LlmProcessingException(
                        f"{self.get_provider_name()} tool call missing arguments",
                        "Ошибка обработки ответа модели.",
                    )
                fn_args = fn["arguments"]
                if isinstance(fn_args, str):
                    try:
                        parsed_args = json.loads(fn_args)
                    except (json.JSONDecodeError, ValueError) as err:
                        raise LlmProcessingException(
                            f"{self.get_provider_name()} malformed tool call arguments JSON",
                            "Ошибка обработки ответа модели.",
                            cause=err,
                        ) from err
                elif isinstance(fn_args, dict):
                    parsed_args = fn_args
                else:
                    raise LlmProcessingException(
                        f"{self.get_provider_name()} tool call arguments must be a JSON object",
                        "Ошибка обработки ответа модели.",
                    )

                if not isinstance(parsed_args, dict):
                    raise LlmProcessingException(
                        f"{self.get_provider_name()} tool call arguments must be a JSON object, got {type(parsed_args).__name__}",
                        "Ошибка обработки ответа модели.",
                    )

                tool_calls.append(ToolCall(name=fn_name, id=tc_id, arguments=parsed_args))

        reasoning_content = message.get("reasoning_content")
        reasoning_str = reasoning_content if isinstance(reasoning_content, str) else None

        resp = LlmResponse(
            text=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_str,
        )

        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE,
                "%s API parsed response (model=%s): text=%s tool_calls=%s reasoning_content=%s",
                self.get_provider_name(),
                self.model,
                content,
                [{"name": tc.name, "id": tc.id, "arguments": tc.arguments} for tc in tool_calls],
                resp.reasoning_content,
            )
        return resp

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
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
            )
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": response.text if response.text else None,
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

        def _parse_counter(val: Any) -> int | None:
            if val is None or isinstance(val, bool):
                return None
            if isinstance(val, int):
                return val if val >= 0 else None
            if isinstance(val, float):
                return int(val) if val >= 0 and val.is_integer() else None
            if isinstance(val, str):
                try:
                    num = int(val)
                    return num if num >= 0 else None
                except ValueError:
                    return None
            return None

        p = _parse_counter(usage.get("prompt_tokens"))
        c = _parse_counter(usage.get("completion_tokens"))
        t = _parse_counter(usage.get("total_tokens"))

        if p is None and c is None and t is None:
            return None

        prompt_tokens = p if p is not None else 0
        completion_tokens = c if c is not None else 0
        total_tokens = t if t is not None else (prompt_tokens + completion_tokens)

        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
