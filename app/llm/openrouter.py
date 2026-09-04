"""OpenRouter chat completions with opaque reasoning replay and envelope errors."""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from app.config import Settings, reveal
from app.llm.base import LlmResponse, is_balance_exhaustion_message
from app.llm.chat_completions import ChatCompletionsClient

if TYPE_CHECKING:
    from app.llm.mcp_router import McpRouter
    from app.rag.service import FaqEmbeddingService
    from app.storage.chat_history import ChatHistoryService
    from app.storage.database import DatabaseSessionManager

logger = logging.getLogger(__name__)

REASONING_EFFORT_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}


def reasoning_parameters(model: str, effort: str) -> dict[str, Any]:
    if model == "z-ai/glm-4.7":
        return {"reasoning": {"enabled": effort != "none"}}
    if model == "z-ai/glm-5.3":
        if effort == "none":
            raise ValueError(
                "OPENROUTER_MODEL=z-ai/glm-5.3 требует reasoning; укажите REASONING_EFFORT=low"
            )
        return {"reasoning": {"effort": REASONING_EFFORT_MAP[effort]}}
    return {}


@dataclass(frozen=True)
class OpenRouterResponse(LlmResponse):
    reasoning_details: list[dict[str, Any]] = field(default_factory=list)


class OpenRouterClient(ChatCompletionsClient):
    PROVIDER_NAME = "OpenRouter"

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
            mcp_router,
            chat_history_service,
            faq_embedding_service,
            db_manager,
            http_client,
            model=settings.openrouter_model or "",
            base_url=settings.openrouter_base_url,
            api_key=reveal(settings.openrouter_api_key),
            request_timeout_seconds=settings.openrouter_timeout_seconds,
        )
        self.reasoning_effort = settings.reasoning_effort
        self._reasoning_parameters = reasoning_parameters(self.model, self.reasoning_effort)
        logger.info(
            "Selected LLM: provider=%s, model=%s, configured_effort=%s, effective_effort=%s",
            self.PROVIDER_NAME,
            self.model,
            self.reasoning_effort,
            self.get_effective_reasoning_effort(),
        )

    def get_effective_reasoning_effort(self) -> str:
        reasoning = self._reasoning_parameters.get("reasoning", {})
        if "enabled" in reasoning:
            return "enabled" if reasoning["enabled"] else "none"
        return str(reasoning.get("effort", "unsupported/ignored"))

    def build_request_body(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {**super().build_request_body(messages), **deepcopy(self._reasoning_parameters)}

    @staticmethod
    def _http_error_code(error: dict[str, Any]) -> int | None:
        code = error.get("code")
        if isinstance(code, str) and code.isascii() and code.isdecimal():
            code = int(code)
        return code if type(code) is int and 400 <= code <= 599 else None

    def check_response_error(
        self, response: httpx.Response | None, payload: dict[str, Any] | None
    ) -> None:
        super().check_response_error(response, payload)
        if payload is None:
            return
        errors = []
        if "error" in payload:
            errors.append(payload["error"])
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            if "error" in choices[0]:
                errors.append(choices[0]["error"])
        if not errors:
            return
        if any(not isinstance(error, dict) for error in errors):
            raise self._error("invalid error envelope")
        codes = [self._http_error_code(error) for error in errors]
        if len(errors) > 1 and (codes[0] != codes[1] or codes[0] is None):
            raise self._error("conflicting error envelopes")
        balance = any(
            isinstance(error.get("message"), str)
            and is_balance_exhaustion_message(error["message"])
            for error in errors
        )
        # Normalization does not override the shared fallback status allowlist.
        raise self._error("API error", status_code=codes[0], fallback_eligible=balance)

    def parse_response(self, payload: dict[str, Any]) -> OpenRouterResponse:
        parsed = super().parse_response(payload)
        message = payload["choices"][0]["message"]
        reasoning = message.get("reasoning")
        details = message.get("reasoning_details")
        if reasoning is not None and not isinstance(reasoning, str):
            raise self._error("invalid reasoning")
        if details is None:
            details = []
        if not isinstance(details, list) or any(not isinstance(item, dict) for item in details):
            raise self._error("invalid reasoning details")
        return OpenRouterResponse(
            text=parsed.text,
            tool_calls=parsed.tool_calls,
            reasoning_content=reasoning,
            reasoning_details=deepcopy(details),
        )

    def add_tool_calls_to_conversation(
        self, conversation: list[dict[str, Any]], response: LlmResponse
    ) -> None:
        super().add_tool_calls_to_conversation(conversation, response)
        if response.reasoning_content is not None:
            conversation[-1]["reasoning"] = response.reasoning_content
        if isinstance(response, OpenRouterResponse) and response.reasoning_details:
            conversation[-1]["reasoning_details"] = deepcopy(response.reasoning_details)
