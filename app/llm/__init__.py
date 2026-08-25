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
from app.llm.gemini import GeminiClient, sanitize_schema_params
from app.llm.mcp_client import (
    HttpMcpClient,
    McpClientInterface,
    McpTool,
)
from app.llm.mcp_router import McpRouter
from app.llm.openai_client import OpenAiClient
from app.llm.prompt import SupportPrompt
from app.llm.rejection import RejectionDetector, is_rejection

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
    """Factory function creating an LlmClient instance based on settings."""
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
    if provider == "openai":
        return OpenAiClient(
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
    "HttpMcpClient",
    "LlmClient",
    "LlmProcessingException",
    "LlmReply",
    "LlmResponse",
    "McpClientInterface",
    "McpRouter",
    "McpTool",
    "OpenAiClient",
    "RejectionDetector",
    "SupportPrompt",
    "ToolCall",
    "create_llm_client",
    "is_rejection",
    "sanitize_schema_params",
]
