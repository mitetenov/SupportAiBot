"""Database connection management and async session handling using SQLAlchemy 2.0."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""

    pass


class DatabaseSessionManager:
    """Manages the async SQLAlchemy engine and session lifecycle."""

    def __init__(
        self,
        database_url: str | None = None,
        echo: bool = False,
        **engine_kwargs: Any,
    ) -> None:
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None
        if database_url:
            self.init(database_url, echo=echo, **engine_kwargs)

    def init(
        self,
        database_url: str,
        echo: bool = False,
        **engine_kwargs: Any,
    ) -> None:
        """Initialize the async engine and session factory."""
        self._engine = create_async_engine(
            database_url,
            echo=echo,
            **engine_kwargs,
        )
        self._sessionmaker = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
        logger.info("DatabaseSessionManager initialized with URL: %s", database_url.split("@")[-1])

    @property
    def is_initialized(self) -> bool:
        """Return True if the engine has been created."""
        return self._engine is not None and self._sessionmaker is not None

    @property
    def engine(self) -> AsyncEngine:
        """Return the active async engine or raise RuntimeError."""
        if self._engine is None:
            raise RuntimeError("DatabaseSessionManager is not initialized. Call init() first.")
        return self._engine

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """Return the active session factory or raise RuntimeError."""
        if self._sessionmaker is None:
            raise RuntimeError("DatabaseSessionManager is not initialized. Call init() first.")
        return self._sessionmaker

    async def close(self) -> None:
        """Dispose the active engine connection pool."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None
            logger.info("DatabaseSessionManager disposed engine connections.")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Provide a transactional async session scope."""
        if self._sessionmaker is None:
            raise RuntimeError("DatabaseSessionManager is not initialized. Call init() first.")

        session: AsyncSession = self._sessionmaker()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def init_models(self) -> None:
        """Create tables if they do not exist and ensure pgvector extension is enabled."""
        if self._engine is None:
            raise RuntimeError("DatabaseSessionManager is not initialized. Call init() first.")

        async with self._engine.begin() as conn:
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception as e:
                logger.warning(
                    "Could not create vector extension (may already exist or unsupported): %s", e
                )
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database models and schema initialized successfully.")


_db_manager_instance: DatabaseSessionManager | None = None


def get_db_manager(database_url: str | None = None) -> DatabaseSessionManager:
    """Return or initialize the global DatabaseSessionManager singleton."""
    global _db_manager_instance
    if _db_manager_instance is None:
        _db_manager_instance = DatabaseSessionManager(database_url)
    elif database_url and not _db_manager_instance.is_initialized:
        _db_manager_instance.init(database_url)
    return _db_manager_instance
