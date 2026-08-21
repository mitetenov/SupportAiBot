"""SQLAlchemy 2.0 ORM models for PostgreSQL and PGVector storage."""

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.database import Base


class User(Base):
    """Telegram user profile tracking."""

    __tablename__ = "user_names"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<User telegram_id={self.telegram_id} username={self.username}>"


class TopicMapping(Base):
    """Maps a Telegram user to their dedicated forum topic thread in the support group."""

    __tablename__ = "topic_mappings"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    topic_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<TopicMapping user_id={self.user_id} topic_id={self.topic_id}>"


class MessageMapping(Base):
    """Maps topic thread message IDs back to user private chat message IDs for reply routing."""

    __tablename__ = "message_mappings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    topic_message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    topic_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Reaction sync looks the pair up together; two single-column indexes served
    # neither query and cost a write each.
    __table_args__ = (
        Index("idx_message_mappings_user_message", "user_chat_id", "user_message_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<MessageMapping id={self.id} topic_message_id={self.topic_message_id} "
            f"user_chat_id={self.user_chat_id} user_message_id={self.user_message_id}>"
        )


class ChatMessage(Base):
    """Persistent chat messages between the user and LLM assistant."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        index=True,
    )

    __table_args__ = (Index("idx_chat_messages_telegram_id", "telegram_id", "created_at"),)

    def __repr__(self) -> str:
        return f"<ChatMessage id={self.id} telegram_id={self.telegram_id} role={self.role}>"


class LlmTokenUsage(Base):
    """Tracks token consumption for LLM API requests."""

    __tablename__ = "llm_token_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<LlmTokenUsage id={self.id} telegram_id={self.telegram_id} total_tokens={self.total_tokens}>"


class KnowledgeGap(Base):
    """Stores queries where the bot failed to find relevant answers or required escalation."""

    __tablename__ = "knowledge_gaps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_query: Mapped[str] = mapped_column(String(2000), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector, nullable=True)
    best_faq_question: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    max_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    faq_count: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    trigger_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    bot_response: Mapped[str | None] = mapped_column(String(500), nullable=True)
    gap_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<KnowledgeGap id={self.id} reason={self.trigger_reason} gap_count={self.gap_count}>"
        )


class Faq(Base):
    """Vectorized FAQ items with full-text search keywords."""

    __tablename__ = "faq"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    question: Mapped[str] = mapped_column(String(2000), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector, nullable=True)
    keywords: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    #: File name under faq/images/, sent to the user after the answer text.
    image: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return f"<Faq id={self.id} question={self.question[:30]}>"


class FaqMetadata(Base):
    """Metadata tracking for FAQ sync status and SHA-256 hash."""

    __tablename__ = "faq_metadata"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    val: Mapped[str] = mapped_column(String(256), nullable=False)

    def __repr__(self) -> str:
        return f"<FaqMetadata key={self.key} val={self.val}>"


class BedolagaTicketState(Base):
    """The last Bedolaga ticket message this bot has answered.

    Both the webhook and the reconciling poll can bring the same ticket in, and
    a delivery may arrive twice — this row is what makes answering a ticket
    idempotent instead of posting the same reply again.
    """

    __tablename__ = "bedolaga_ticket_state"

    ticket_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    last_answered_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<BedolagaTicketState ticket_id={self.ticket_id} "
            f"last_answered_message_id={self.last_answered_message_id}>"
        )
