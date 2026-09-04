from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from app.config import Settings, reveal
from app.llm.base import LlmProcessingException, LlmResponse, is_balance_exhaustion_message
from app.llm.chat_completions import ChatCompletionsClient
from app.llm.fallback import _FALLBACK_STATUS_CODES

if TYPE_CHECKING:
    from app.llm.mcp_router import McpRouter
    from app.rag.service import FaqEmbeddingService
    from app.storage.chat_history import ChatHistoryService
    from app.storage.database import DatabaseSessionManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenRouterResponse(LlmResponse):
    """OpenRouter response containing structured reasoning details."""

    reasoning_details: list[dict[str, Any]] = field(default_factory=list)


class OpenRouterClient(ChatCompletionsClient):
    """Client for OpenRouter API using OpenAI-compatible /chat/completions."""

    def __init__(
        self,
        settings: Settings,
        mcp_router: McpRouter,
        chat_history_service: ChatHistoryService,
        faq_embedding_service: FaqEmbeddingService,
        db_manager: DatabaseSessionManager | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        model = settings.openrouter_model or ""
        normalized_model = model.strip().lower()

        if normalized_model == "z-ai/glm-5.3" and settings.reasoning_effort == "none":
            raise ValueError(
                f"Модель {model} провайдера {self.get_provider_name()} не поддерживает "
                "REASONING_EFFORT='none' (reasoning обязателен). Выберите 'low', 'high' или 'max'."
            )

        super().__init__(
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_embedding_service=faq_embedding_service,
            db_manager=db_manager,
            http_client=http_client,
            model=model,
            base_url=settings.openrouter_base_url,
            api_key=reveal(settings.openrouter_api_key),
            request_timeout_seconds=settings.openrouter_timeout_seconds,
        )
        self._log_reasoning_configuration()

    def _log_reasoning_configuration(self) -> None:
        logger.info(
            "Selected LLM: provider=%s, model=%s, configured_effort=%s, effective_effort=%s",
            self.get_provider_name(),
            self.model,
            self.settings.reasoning_effort,
            self.get_effective_reasoning_effort(),
        )

    def get_provider_name(self) -> str:
        return "OpenRouter"

    def get_effective_reasoning_effort(self) -> str:
        """Return the effective reasoning effort mode for the configured model."""
        normalized_model = self.model.strip().lower()
        effort = self.settings.reasoning_effort

        if normalized_model == "z-ai/glm-4.7":
            if effort == "none":
                return "none"
            return "enabled"

        if normalized_model == "z-ai/glm-5.3":
            if effort in ("minimal", "low"):
                return "low"
            if effort in ("medium", "high"):
                return "high"
            if effort in ("xhigh", "max"):
                return "max"
            return "none"

        return "unsupported/ignored"

    def build_request_body(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Build chat completions body with OpenRouter reasoning configuration."""
        body = super().build_request_body(messages)
        normalized_model = self.model.strip().lower()
        effort = self.settings.reasoning_effort

        if normalized_model == "z-ai/glm-4.7":
            if effort == "none":
                body["reasoning"] = {"enabled": False}
            else:
                body["reasoning"] = {"enabled": True}
        elif normalized_model == "z-ai/glm-5.3":
            effective = self.get_effective_reasoning_effort()
            body["reasoning"] = {"effort": effective}
        else:
            logger.info(
                "Reasoning unsupported/ignored for provider=%s model=%s (configured effort=%s)",
                self.get_provider_name(),
                self.model,
                effort,
            )

        return body

    def parse_response(self, payload: dict[str, Any]) -> OpenRouterResponse:
        """Parse chat completions response into OpenRouterResponse with reasoning."""
        base_response = super().parse_response(payload)

        # super().parse_response strictly validates choices and choices[0].message
        message: dict[str, Any] = payload["choices"][0]["message"]

        reasoning_str = message.get("reasoning")
        if not isinstance(reasoning_str, str):
            reasoning_str = base_response.reasoning_content

        raw_details = message.get("reasoning_details")
        reasoning_details: list[dict[str, Any]] = []
        if isinstance(raw_details, list):
            for item in raw_details:
                if isinstance(item, dict):
                    reasoning_details.append(item)

        return OpenRouterResponse(
            text=base_response.text,
            tool_calls=base_response.tool_calls,
            raw_parts=base_response.raw_parts,
            reasoning_content=reasoning_str,
            reasoning_details=reasoning_details,
        )

    def add_tool_calls_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        response: LlmResponse,
    ) -> None:
        """Append assistant tool call message preserving OpenRouter reasoning tokens."""
        super().add_tool_calls_to_conversation(conversation, response)
        assistant_message = conversation[-1]
        assistant_message.pop("reasoning_content", None)
        if response.reasoning_content is not None:
            assistant_message["reasoning"] = response.reasoning_content
        if isinstance(response, OpenRouterResponse) and response.reasoning_details:
            assistant_message["reasoning_details"] = response.reasoning_details

    def check_response_error(
        self, response: httpx.Response, payload: dict[str, Any] | None
    ) -> None:
        """Inspect HTTP response and OpenRouter error payload for errors."""
        raw_msg = ""
        error_node: Any = None
        has_error = False
        if payload and "error" in payload and payload["error"] is not None:
            has_error = True
            error_node = payload["error"]
        elif payload and "error" in payload:
            has_error = True
        elif payload:
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                has_error = "error" in choices[0]
                error_node = choices[0].get("error")
        if isinstance(error_node, (dict, str)):
            err = error_node
            raw_msg = str(err.get("message") if isinstance(err, dict) else err)
        elif response.text:
            raw_msg = response.text

        is_balance = is_balance_exhaustion_message(raw_msg)

        if response.status_code >= 400:
            http_status = response.status_code
            fallback_eligible = (http_status in _FALLBACK_STATUS_CODES) or is_balance
            raise LlmProcessingException(
                f"{self.get_provider_name()} API error (model={self.model}, status={http_status})",
                "Произошла ошибка при обработке запроса. Попробуйте позже.",
                status_code=http_status,
                fallback_eligible=fallback_eligible,
            )

        if has_error:
            if error_node is None:
                raise LlmProcessingException(
                    f"{self.get_provider_name()} invalid error envelope",
                    "Ошибка обработки ответа модели.",
                )
            err = error_node
            if not isinstance(err, dict):
                raise LlmProcessingException(
                    f"{self.get_provider_name()} invalid error envelope",
                    "Ошибка обработки ответа модели.",
                )
            raw_code = err.get("code") if isinstance(err, dict) else None

            normalized_code: int | None = None
            if isinstance(raw_code, int):
                normalized_code = raw_code
            elif isinstance(raw_code, str) and raw_code.strip().isdigit():
                try:
                    normalized_code = int(raw_code.strip())
                except ValueError:
                    normalized_code = None

            status_code: int | None = (
                normalized_code
                if (normalized_code is not None and 100 <= normalized_code <= 599)
                else None
            )
            fallback_eligible = (
                (status_code in _FALLBACK_STATUS_CODES) if status_code is not None else False
            ) or is_balance

            desc = f"{self.get_provider_name()} API error (model={self.model}"
            if status_code is not None:
                desc += f", status={status_code})"
            elif raw_code is not None:
                desc += f", code={raw_code})"
            else:
                desc += ")"

            raise LlmProcessingException(
                desc,
                "Произошла ошибка при обработке запроса. Попробуйте позже.",
                status_code=status_code,
                fallback_eligible=fallback_eligible,
            )
