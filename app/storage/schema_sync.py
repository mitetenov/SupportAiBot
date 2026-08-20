"""Reconciles tables an earlier version of this service created.

``Base.metadata.create_all`` creates missing tables and never touches existing
ones, so a database first populated by the Java service keeps that service's
column set forever. Two of those differences break writes outright:

* ``message_mappings`` has no ``created_at`` at all — the Java entity had no
  such field — so every insert fails and operator replies lose the link back to
  the user's original message.
* several timestamp columns are ``timestamp without time zone``. The ORM adapts,
  but the raw SQL used for knowledge gaps hands asyncpg an aware ``datetime``
  that Postgres has declared naive, and the insert is rejected.

Everything here is idempotent, so it runs on every startup and does nothing at
all on a database that is already current.
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

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

NAIVE_TIMESTAMP = "timestamp without time zone"


async def sync_legacy_schema(engine: AsyncEngine) -> list[str]:
    """Bring pre-existing tables in line with the current models.

    Returns the list of changes applied, empty when nothing needed doing.
    """
    applied: list[str] = []

    async with engine.begin() as conn:
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

    if applied:
        logger.info("Reconciled legacy schema: %s", "; ".join(applied))
    else:
        logger.debug("Schema already current, nothing to reconcile")
    return applied
