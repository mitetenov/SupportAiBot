from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from app.config import Settings
from app.llm.base import (
    AbstractLlmClient,
    LlmClient,
    LlmProcessingException,
    LlmReply,
    LlmResponse,
    ToolCall,
)
from app.llm.deepseek import DeepSeekClient
from app.llm.escalation import EscalationPolicy
from app.llm.fallback import LlmFallbackClient, LlmFallbackExhaustedError
from app.llm.gemini import GeminiClient, sanitize_schema_params
from app.llm.groq import GroqClient
from app.llm.mcp_client import (
    HttpMcpClient,
    McpClientInterface,
    McpTool,
)
from app.llm.mcp_router import McpRouter
from app.llm.openai_client import OpenAiClient
from app.llm.openrouter import OpenRouterClient, OpenRouterResponse
from app.llm.prompt import SupportPrompt
from app.llm.rejection import RejectionDetector, is_rejection
from app.llm.zai import ZaiClient

if TYPE_CHECKING:
    from app.rag.service import FaqEmbeddingService
    from app.storage.chat_history import ChatHistoryService
    from app.storage.database import DatabaseSessionManager


def create_llm_client(
    settings: Settings,
    mcp_router: McpRouter,
    chat_history_service: ChatHistoryService,
    faq_service: FaqEmbeddingService,
    db_manager: DatabaseSessionManager | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> LlmClient:
    """Create one provider client or an ordered fallback coordinator."""
    targets = settings.llm_provider_targets
    clients = [
        _create_provider_client(
            settings.model_copy(
                update={"llm_provider": target.provider, f"{target.provider}_model": target.model}
            ),
            mcp_router,
            chat_history_service,
            faq_service,
            db_manager,
            http_client,
        )
        for target in targets
    ]
    return clients[0] if len(clients) == 1 else LlmFallbackClient(clients)


def _create_provider_client(
    settings: Settings,
    mcp_router: McpRouter,
    chat_history_service: ChatHistoryService,
    faq_service: FaqEmbeddingService,
    db_manager: DatabaseSessionManager | None,
    http_client: httpx.AsyncClient | None,
) -> AbstractLlmClient:
    """Create exactly one concrete provider client from one validated target."""
    provider = (settings.llm_provider or "").strip().lower()
    if provider == "gemini":
        return GeminiClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_embedding_service=faq_service,
            db_manager=db_manager,
            http_client=http_client,
        )
    if provider == "deepseek":
        return DeepSeekClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_embedding_service=faq_service,
            db_manager=db_manager,
            http_client=http_client,
        )
    if provider == "groq":
        return GroqClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_embedding_service=faq_service,
            db_manager=db_manager,
            http_client=http_client,
        )
    if provider == "openai":
        return OpenAiClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_embedding_service=faq_service,
            db_manager=db_manager,
            http_client=http_client,
        )
    if provider == "openrouter":
        return OpenRouterClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_embedding_service=faq_service,
            db_manager=db_manager,
            http_client=http_client,
        )
    if provider == "zai":
        return ZaiClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_embedding_service=faq_service,
            db_manager=db_manager,
            http_client=http_client,
        )
    raise ValueError(f"Unknown LLM provider: {settings.llm_provider}")


__all__ = [
    "AbstractLlmClient",
    "DeepSeekClient",
    "EscalationPolicy",
    "GeminiClient",
    "GroqClient",
    "HttpMcpClient",
    "LlmClient",
    "LlmFallbackClient",
    "LlmFallbackExhaustedError",
    "LlmProcessingException",
    "LlmReply",
    "LlmResponse",
    "McpClientInterface",
    "McpRouter",
    "McpTool",
    "OpenAiClient",
    "OpenRouterClient",
    "OpenRouterResponse",
    "RejectionDetector",
    "SupportPrompt",
    "ToolCall",
    "ZaiClient",
    "create_llm_client",
    "is_rejection",
    "sanitize_schema_params",
]
