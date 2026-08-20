"""Storage layer package providing async database models, connection management, and chat history."""

from app.storage.chat_history import ChatHistoryService
from app.storage.database import Base, DatabaseSessionManager, get_db_manager
from app.storage.models import (
    ChatMessage,
    Faq,
    FaqMetadata,
    KnowledgeGap,
    LlmTokenUsage,
    MessageMapping,
    TopicMapping,
    User,
)
from app.storage.schema import sync_legacy_schema

__all__ = [
    "Base",
    "ChatHistoryService",
    "ChatMessage",
    "DatabaseSessionManager",
    "Faq",
    "FaqMetadata",
    "KnowledgeGap",
    "LlmTokenUsage",
    "MessageMapping",
    "TopicMapping",
    "User",
    "get_db_manager",
    "sync_legacy_schema",
]
