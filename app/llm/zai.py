"""Z.AI native reasoning controls and business error codes."""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx

from app.config import Settings, reveal
from app.llm.base import LlmResponse
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
    if model == "glm-4.7":
        return {"thinking": {"type": "disabled" if effort == "none" else "enabled"}}
    if model == "glm-5.3":
        if effort == "none":
            raise ValueError("ZAI_MODEL=glm-5.3 требует reasoning; укажите REASONING_EFFORT=low")
        return {"thinking": {"type": "enabled"}, "reasoning_effort": REASONING_EFFORT_MAP[effort]}
    return {}


class ZaiClient(ChatCompletionsClient):
    PROVIDER_NAME = "Z.AI"

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
            model=settings.zai_model or "",
            base_url=settings.zai_base_url,
            api_key=reveal(settings.zai_api_key),
            request_timeout_seconds=settings.zai_timeout_seconds,
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
        if self._reasoning_parameters.get("thinking", {}).get("type") == "disabled":
            return "none"
        return str(
            self._reasoning_parameters.get(
                "reasoning_effort",
                self._reasoning_parameters.get("thinking", {}).get("type", "unsupported/ignored"),
            )
        )

    def build_request_body(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {**super().build_request_body(messages), **deepcopy(self._reasoning_parameters)}

    def check_response_error(
        self, response: httpx.Response | None, payload: dict[str, Any] | None
    ) -> None:
        super().check_response_error(response, payload)
        if payload is None:
            return
        if "error" in payload:
            error = payload["error"]
        elif "code" in payload and "message" in payload and "choices" not in payload:
            error = payload
        else:
            return
        if not isinstance(error, dict):
            raise self._error("invalid error envelope")
        code = error.get("code")
        quota = (type(code) is int and code == 1113) or code == "1113"
        raise self._error("API error", fallback_eligible=quota)

    def _validate_finish_reason(self, choice: dict[str, Any]) -> None:
        if choice.get("finish_reason") == "network_error":
            raise self._error("provider network error", fallback_eligible=True)
        super()._validate_finish_reason(choice)

    def parse_response(self, payload: dict[str, Any]) -> LlmResponse:
        parsed = super().parse_response(payload)
        reasoning = payload["choices"][0]["message"].get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise self._error("invalid reasoning content")
        return replace(parsed, reasoning_content=reasoning)

    def add_tool_calls_to_conversation(
        self, conversation: list[dict[str, Any]], response: LlmResponse
    ) -> None:
        super().add_tool_calls_to_conversation(conversation, response)
        if response.reasoning_content is not None:
            conversation[-1]["reasoning_content"] = response.reasoning_content
