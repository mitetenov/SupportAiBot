from __future__ import annotations

import logging
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


class ZaiClient(ChatCompletionsClient):
    """Client for Z.AI API using OpenAI-compatible /chat/completions."""

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
        model = settings.zai_model or ""
        normalized_model = model.strip().lower()

        if normalized_model == "glm-5.3" and settings.reasoning_effort == "none":
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
            base_url=settings.zai_base_url,
            api_key=reveal(settings.zai_api_key),
            request_timeout_seconds=settings.zai_timeout_seconds,
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
        return "Z.AI"

    def get_effective_reasoning_effort(self) -> str:
        """Return the effective reasoning effort mode for the configured model."""
        normalized_model = self.model.strip().lower()
        effort = self.settings.reasoning_effort

        if normalized_model in ("glm-4.7", "glm-5.3-flash"):
            if effort == "none":
                return "none"
            return "enabled"

        if normalized_model == "glm-5.3":
            if effort in ("minimal", "low"):
                return "low"
            if effort in ("medium", "high"):
                return "high"
            if effort in ("xhigh", "max"):
                return "max"
            return "none"

        return "unsupported/ignored"

    def build_request_body(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Build chat completions body with Z.AI reasoning configuration."""
        body = super().build_request_body(messages)
        normalized_model = self.model.strip().lower()
        effort = self.settings.reasoning_effort

        if normalized_model in ("glm-4.7", "glm-5.3-flash"):
            if effort == "none":
                body["thinking"] = {"type": "disabled"}
            else:
                body["thinking"] = {"type": "enabled"}
        elif normalized_model == "glm-5.3":
            effective = self.get_effective_reasoning_effort()
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = effective
        else:
            logger.info(
                "Reasoning unsupported/ignored for provider=%s model=%s (configured effort=%s)",
                self.get_provider_name(),
                self.model,
                effort,
            )

        return body

    def parse_response(self, payload: dict[str, Any]) -> LlmResponse:
        """Parse chat completions response into LlmResponse with reasoning_content."""
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            if choices[0].get("finish_reason") == "network_error":
                raise self._network_finish_error()
        return super().parse_response(payload)

    def _network_finish_error(self) -> LlmProcessingException:
        return LlmProcessingException(
            f"{self.get_provider_name()} provider network error",
            "Произошла ошибка при обработке запроса. Попробуйте позже.",
            fallback_eligible=True,
        )

    def add_tool_calls_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        response: LlmResponse,
    ) -> None:
        """Append assistant tool call message preserving Z.AI reasoning_content."""
        super().add_tool_calls_to_conversation(conversation, response)

    def check_response_error(
        self, response: httpx.Response, payload: dict[str, Any] | None
    ) -> None:
        """Inspect HTTP response and Z.AI business error codes."""
        raw_code: Any = None
        raw_msg = ""
        has_error = False

        if payload is not None and isinstance(payload, dict):
            if "error" in payload and payload["error"] is not None:
                has_error = True
                err = payload["error"]
                if isinstance(err, dict):
                    raw_code = err.get("code")
                    raw_msg = str(err.get("message") or "")
                else:
                    raw_msg = str(err)
            elif "code" in payload and payload["code"] not in (None, 0, 200, "0", "200"):
                has_error = True
                raw_code = payload["code"]
                raw_msg = str(payload.get("msg") or payload.get("message") or "")
        elif response.text:
            raw_msg = response.text

        normalized_code: int | None = None
        if isinstance(raw_code, int):
            normalized_code = raw_code
        elif isinstance(raw_code, str) and raw_code.strip().isdigit():
            try:
                normalized_code = int(raw_code.strip())
            except ValueError:
                normalized_code = None

        is_balance = (normalized_code == 1113) or is_balance_exhaustion_message(raw_msg)

        if response.status_code >= 400:
            status_code = response.status_code
            fallback_eligible = (status_code in _FALLBACK_STATUS_CODES) or is_balance

            desc = f"{self.get_provider_name()} API error (model={self.model}, status={status_code}"
            if normalized_code is not None:
                desc += f", code={normalized_code})"
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

        if has_error:
            # At HTTP 200, do NOT record business code (like 1113 or 1210) into status_code!
            fallback_eligible = is_balance

            desc = f"{self.get_provider_name()} API error (model={self.model}"
            if normalized_code is not None:
                desc += f", code={normalized_code})"
            elif raw_code is not None:
                desc += f", code={raw_code})"
            else:
                desc += ")"

            raise LlmProcessingException(
                desc,
                "Произошла ошибка при обработке запроса. Попробуйте позже.",
                status_code=None,
                fallback_eligible=fallback_eligible,
            )
