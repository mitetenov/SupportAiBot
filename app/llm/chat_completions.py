"""Shared, non-streaming Chat Completions transport for new providers."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import httpx

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
_RAW_REASONING = re.compile(r"<think\b[^>]*>.*?(?:</think\s*>|$)", re.DOTALL | re.IGNORECASE)


class ChatCompletionsClient(AbstractLlmClient):
    """Provider-independent messages, tool validation and HTTP client ownership."""

    PROVIDER_NAME = "Chat Completions"

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
        request_timeout_seconds: float = 120,
    ) -> None:
        super().__init__(mcp_router, chat_history_service, faq_embedding_service, db_manager)
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.request_timeout_seconds = request_timeout_seconds
        self._http_client = http_client
        self._own_client = False
        self.tool_definitions = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema or {"type": "object", "properties": {}},
                },
            }
            for tool in mcp_router.list_tools()
        ]

    def get_provider_name(self) -> str:
        return self.PROVIDER_NAME

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(event_hooks=create_logging_hooks())
            self._own_client = True
        return self._http_client

    async def close(self) -> None:
        if self._own_client and self._http_client is not None:
            await self._http_client.aclose()

    def build_request_body(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False}
        if self.tool_definitions:
            body.update(tools=self.tool_definitions, tool_choice="auto")
        return body

    def build_initial_conversation(
        self,
        user_message: str,
        telegram_user_id: int,
        faq_context: str,
        base64_image: str | None = None,
        mime_type: str | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if base64_image is not None:
            raise LlmProcessingException("Image not supported")
        return [
            {"role": "system", "content": SupportPrompt.SYSTEM},
            {
                "role": "system",
                "content": SupportPrompt.dynamic_context(faq_context, telegram_user_id),
            },
            *(history or []),
            {"role": "user", "content": user_message},
        ]

    def _error(self, reason: str, **kwargs: Any) -> LlmProcessingException:
        # Only fixed diagnostic labels/codes belong here, never provider text.
        logger.error(
            "%s API failure (model=%s, status=%s): %s",
            self.PROVIDER_NAME,
            self.model,
            kwargs.get("status_code"),
            reason,
        )
        return LlmProcessingException(f"{self.PROVIDER_NAME}: {reason}", **kwargs)

    def check_response_error(
        self, response: httpx.Response | None, payload: dict[str, Any] | None
    ) -> None:
        if response is not None and response.status_code >= 400:
            raise self._error(
                f"HTTP {response.status_code}",
                status_code=response.status_code,
                fallback_eligible=is_balance_exhaustion_message(response.text),
            )

    async def call_api(
        self, conversation: list[dict[str, Any]], faq_context: str, telegram_user_id: int
    ) -> dict[str, Any]:
        body = self.build_request_body(conversation)
        if logger.isEnabledFor(TRACE):
            logger.log(TRACE, "%s request: %s", self.PROVIDER_NAME, safe_serialize(body))
        try:
            response = await post_with_retry(
                self.http_client,
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.request_timeout_seconds,
                description=f"{self.PROVIDER_NAME} API",
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise self._error("transport failure", cause=exc) from exc
        if logger.isEnabledFor(TRACE):
            logger.log(TRACE, "%s response: %s", self.PROVIDER_NAME, safe_serialize(response.text))
        # Status wins even for HTML or invalid JSON. Decode successful responses once.
        self.check_response_error(response, None)
        try:
            payload = response.json()
        except ValueError:
            raise self._error("invalid JSON response") from None
        if not isinstance(payload, dict):
            raise self._error("response must be an object")
        self.check_response_error(response, payload)
        self._validate_finish_reason(self._choice(payload))
        return payload

    def _choice(self, payload: dict[str, Any]) -> dict[str, Any]:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise self._error("missing or invalid choices")
        return choices[0]

    def _validate_finish_reason(self, choice: dict[str, Any]) -> None:
        message = choice.get("message")
        if not isinstance(message, dict):
            raise self._error("missing or invalid message")
        finish = choice.get("finish_reason")
        calls = message.get("tool_calls")
        if finish == "stop" and not calls:
            return
        if finish == "tool_calls" and isinstance(calls, list) and calls:
            return
        raise self._error("incomplete or inconsistent completion")

    def parse_response(self, payload: dict[str, Any]) -> LlmResponse:
        self.check_response_error(None, payload)
        choice = self._choice(payload)
        self._validate_finish_reason(choice)
        message = choice["message"]
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise self._error("invalid content")
        content = _RAW_REASONING.sub("", content or "").strip()
        raw_calls = message.get("tool_calls")
        if raw_calls is None:
            raw_calls = []
        if not isinstance(raw_calls, list):
            raise self._error("invalid tool calls")
        calls: list[ToolCall] = []
        seen_ids: set[str] = set()
        for raw in raw_calls:
            if not isinstance(raw, dict) or raw.get("type", "function") != "function":
                raise self._error("invalid tool call")
            call_id, function = raw.get("id"), raw.get("function")
            if not isinstance(call_id, str) or not call_id.strip() or call_id in seen_ids:
                raise self._error("missing or duplicate tool call id")
            if not isinstance(function, dict):
                raise self._error("invalid tool function")
            name, arguments = function.get("name"), function.get("arguments")
            if not isinstance(name, str) or not name.strip():
                raise self._error("invalid tool name")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    raise self._error("invalid tool arguments JSON") from None
            if not isinstance(arguments, dict):
                raise self._error("tool arguments must be an object")
            seen_ids.add(call_id)
            calls.append(ToolCall(name=name, id=call_id, arguments=arguments))
        if not calls and not (content or "").strip():
            raise self._error("empty completion")
        return LlmResponse(text=content or "", tool_calls=calls)

    def add_tool_calls_to_conversation(
        self, conversation: list[dict[str, Any]], response: LlmResponse
    ) -> None:
        conversation.append(
            {
                "role": "assistant",
                "content": response.text or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in response.tool_calls
                ],
            }
        )

    def add_tool_result_to_conversation(
        self, conversation: list[dict[str, Any]], tool_call: ToolCall, tool_result: str
    ) -> None:
        conversation.append({"role": "tool", "tool_call_id": tool_call.id, "content": tool_result})

    def extract_usage(self, payload: dict[str, Any]) -> TokenUsage | None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        prompt, completion = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        if any(type(value) is not int or value < 0 for value in (prompt, completion)):
            return None
        total = usage.get("total_tokens", prompt + completion)
        if type(total) is not int or total < 0:
            return None
        return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)
