"""Every piece of DDL this service issues at runtime.

The ORM models are the source of truth for tables and columns:
``Base.metadata.create_all`` creates them. Three things it cannot express live
here instead, and nowhere else:

* the **vector dimension**, which is not known until the configured embedding
  provider is instantiated;
* the **search indices** — HNSW over the embedding, GIN over the Russian
  full-text expression;
* **reconciliation** of a database an earlier version of this service (or the
  Java one) created, which ``create_all`` never touches.

Keeping all of it in one module is what stops the table definitions from
drifting apart: before, ``faq`` was declared once as an ORM model and again as
a raw ``CREATE TABLE`` with a different ``answer`` type, and the full-text
expression was spelled out separately in the index and in the query that was
supposed to use it.
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

logger = logging.getLogger(__name__)

#: The document the Russian full-text index is built over. The search query has
#: to repeat this expression verbatim or Postgres will not use the index, so
#: both sides read it from here.
FAQ_FTS_EXPRESSION = (
    "to_tsvector('russian', question || ' ' || COALESCE(keywords, '') || ' ' || answer)"
)

# pgvector stores the declared dimension in atttypmod; -1 means it was declared
# without one.
_COLUMN_DIMENSION_SQL = text("""
    SELECT atttypmod
    FROM pg_attribute
    WHERE attrelid = CAST(:table_name AS regclass)
      AND attname = :column_name
      AND NOT attisdropped
""")

_COLUMN_TYPE_SQL = text("""
    SELECT data_type
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = :table_name
      AND column_name = :column_name
""")

_TABLE_EXISTS_SQL = text("""
    SELECT 1
    FROM information_schema.tables
    WHERE table_schema = current_schema() AND table_name = :table_name
""")

# Every column the models declare as DateTime(timezone=True). Both services
# wrote UTC, so naive values are reinterpreted as UTC rather than as local time.
UTC_TIMESTAMP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("topic_mappings", "created_at"),
    ("message_mappings", "created_at"),
    ("chat_messages", "created_at"),
    ("llm_token_usage", "created_at"),
    ("user_names", "updated_at"),
    ("knowledge_gaps", "first_seen"),
    ("knowledge_gaps", "last_seen"),
)

# `create_all` creates the Bedolaga state table on a fresh install, but it does
# not add columns to an existing one. The first version of the integration only
# stored only the user-message watermark. The bot now also records its own reply
# id and the latest human reply id, so an admin message written in Bedolaga's
# panel can be recognised and can refresh a finite ownership window.
BEDOLAGA_STATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("bedolaga_ticket_state", "last_bot_reply_message_id"),
    ("bedolaga_ticket_state", "last_human_reply_message_id"),
)

# Unlike the legacy timestamp columns above, this field means "no human reply"
# when it is NULL. Giving old rows DEFAULT now() would manufacture operator
# activity, and NOT NULL would reject every ordinary bot reply that persists
# None. It therefore has a deliberately separate reconciliation path.
NULLABLE_UTC_TIMESTAMP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("bedolaga_ticket_state", "last_human_reply_at"),
)

NAIVE_TIMESTAMP = "timestamp without time zone"


async def ensure_vector_extension(session: AsyncSession | AsyncConnection) -> None:
    """Enable pgvector, tolerating a role that is not allowed to."""
    try:
        await session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception as e:
        logger.warning("Could not create vector extension (may already exist): %s", e)


async def ensure_vector_column(
    session: AsyncSession | AsyncConnection,
    table_name: str,
    column_name: str,
    dimension: int,
) -> bool:
    """Make sure the column exists as vector(dimension), preserving existing values.

    Returns True when the column was created or rebuilt — meaning whatever it
    held is gone and the caller has to re-embed.

    The previous DROP-then-ADD ran on every startup and silently emptied the
    column each time. Paired with a hash check that skipped re-indexing whenever
    the source file was unchanged, that left every row with a NULL embedding for
    the entire life of the deployment.
    """
    result = await session.execute(
        _COLUMN_DIMENSION_SQL, {"table_name": table_name, "column_name": column_name}
    )
    row = result.fetchone()

    if row is not None and int(row[0]) == dimension:
        logger.debug(
            "%s.%s already vector(%d) — keeping existing embeddings",
            table_name,
            column_name,
            dimension,
        )
        return False

    if row is not None:
        logger.warning(
            "%s.%s is vector(%s) but the embedding provider produces %d dimensions — "
            "rebuilding the column, every row will be re-embedded",
            table_name,
            column_name,
            row[0] if int(row[0]) > 0 else "unspecified",
            dimension,
        )
        await session.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))

    await session.execute(
        text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} vector({dimension})")
    )
    logger.info("Added %s.%s as vector(%d)", table_name, column_name, dimension)
    return True


async def ensure_hnsw_index(
    session: AsyncSession | AsyncConnection,
    table_name: str,
    column_name: str,
) -> None:
    """Create the cosine HNSW index, without which every search is a full scan."""
    try:
        await session.execute(
            text(f"""
            CREATE INDEX IF NOT EXISTS {table_name}_{column_name}_idx
            ON {table_name} USING hnsw ({column_name} vector_cosine_ops)
            """)
        )
    except Exception as e:
        logger.warning("Could not create HNSW index on %s.%s: %s", table_name, column_name, e)


async def ensure_faq_search_schema(session: AsyncSession, dimension: int) -> bool:
    """Bring the ``faq`` table up to what hybrid search needs.

    Returns True when the embedding column was rebuilt and the rows have to be
    re-indexed.
    """
    await ensure_vector_extension(session)
    rebuilt = await ensure_vector_column(session, "faq", "embedding", dimension)

    try:
        # "images" is a column the Java service carried that nothing reads any
        # more. "image" is read again — an entry names a screenshot to send with
        # its answer — so it is added here for databases created before that.
        await session.execute(text("ALTER TABLE faq DROP COLUMN IF EXISTS images"))
        await session.execute(text("ALTER TABLE faq ADD COLUMN IF NOT EXISTS image VARCHAR(255)"))
    except Exception as e:
        logger.warning("Could not reconcile FAQ columns: %s", e)

    await ensure_hnsw_index(session, "faq", "embedding")
    try:
        await session.execute(
            text(f"CREATE INDEX IF NOT EXISTS faq_fts_idx ON faq USING gin ({FAQ_FTS_EXPRESSION})")
        )
    except Exception as e:
        logger.warning("Could not create FAQ full-text index: %s", e)

    return rebuilt


async def ensure_knowledge_gap_schema(session: AsyncSession, dimension: int) -> bool:
    """Bring the ``knowledge_gaps`` table up to what deduplication needs."""
    await ensure_vector_extension(session)
    rebuilt = await ensure_vector_column(session, "knowledge_gaps", "embedding", dimension)
    await ensure_hnsw_index(session, "knowledge_gaps", "embedding")
    return rebuilt


async def sync_legacy_schema(engine: AsyncEngine) -> list[str]:
    """Bring pre-existing tables in line with the current models.

    ``create_all`` creates missing tables and never touches existing ones, so a
    database first populated by the Java service keeps that service's column set
    forever. Two of those differences break writes outright:

    * ``message_mappings`` has no ``created_at`` at all — the Java entity had no
      such field — so every insert fails and operator replies lose the link back
      to the user's original message.
    * several timestamp columns are ``timestamp without time zone``. The ORM
      adapts, but the raw SQL used for knowledge gaps hands asyncpg an aware
      ``datetime`` that Postgres has declared naive, and the insert is rejected.

    Everything here is idempotent, so it runs on every startup and does nothing
    at all on a database that is already current. Returns the list of changes
    applied, empty when nothing needed doing.
    """
    applied: list[str] = []

    async with engine.begin() as conn:
        for table_name, column_name in BEDOLAGA_STATE_COLUMNS:
            exists = await conn.execute(_TABLE_EXISTS_SQL, {"table_name": table_name})
            if exists.fetchone() is None:
                continue

            result = await conn.execute(
                _COLUMN_TYPE_SQL, {"table_name": table_name, "column_name": column_name}
            )
            if result.fetchone() is not None:
                continue

            try:
                await conn.execute(
                    text(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} "
                        "BIGINT NOT NULL DEFAULT 0"
                    )
                )
                applied.append(f"{table_name}.{column_name}: added")
            except Exception as e:
                logger.warning("Could not reconcile %s.%s: %s", table_name, column_name, e)

        for table_name, column_name in UTC_TIMESTAMP_COLUMNS:
            exists = await conn.execute(_TABLE_EXISTS_SQL, {"table_name": table_name})
            if exists.fetchone() is None:
                continue

            result = await conn.execute(
                _COLUMN_TYPE_SQL, {"table_name": table_name, "column_name": column_name}
            )
            row = result.fetchone()

            try:
                if row is None:
                    await conn.execute(
                        text(
                            f"ALTER TABLE {table_name} "
                            f"ADD COLUMN {column_name} TIMESTAMPTZ NOT NULL DEFAULT now()"
                        )
                    )
                    applied.append(f"{table_name}.{column_name}: added")
                elif row[0] == NAIVE_TIMESTAMP:
                    await conn.execute(
                        text(
                            f"ALTER TABLE {table_name} "
                            f"ALTER COLUMN {column_name} TYPE TIMESTAMPTZ "
                            f"USING {column_name} AT TIME ZONE 'UTC'"
                        )
                    )
                    applied.append(f"{table_name}.{column_name}: timestamp -> timestamptz")
            except Exception as e:
                logger.warning("Could not reconcile %s.%s: %s", table_name, column_name, e)

        for table_name, column_name in NULLABLE_UTC_TIMESTAMP_COLUMNS:
            exists = await conn.execute(_TABLE_EXISTS_SQL, {"table_name": table_name})
            if exists.fetchone() is None:
                continue

            result = await conn.execute(
                _COLUMN_TYPE_SQL, {"table_name": table_name, "column_name": column_name}
            )
            row = result.fetchone()

            try:
                if row is None:
                    await conn.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} TIMESTAMPTZ NULL")
                    )
                    applied.append(f"{table_name}.{column_name}: added")
                elif row[0] == NAIVE_TIMESTAMP:
                    await conn.execute(
                        text(
                            f"ALTER TABLE {table_name} "
                            f"ALTER COLUMN {column_name} TYPE TIMESTAMPTZ "
                            f"USING {column_name} AT TIME ZONE 'UTC'"
                        )
                    )
                    applied.append(f"{table_name}.{column_name}: timestamp -> timestamptz")
            except Exception as e:
                logger.warning("Could not reconcile %s.%s: %s", table_name, column_name, e)

    if applied:
        logger.info("Reconciled legacy schema: %s", "; ".join(applied))
    else:
        logger.debug("Schema already current, nothing to reconcile")
    return applied
