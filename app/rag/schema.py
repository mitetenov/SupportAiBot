"""Vector-column DDL that keeps the data it already has."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# pgvector stores the declared dimension in atttypmod; -1 means it was declared
# without one.
_COLUMN_DIMENSION_SQL = text("""
    SELECT atttypmod
    FROM pg_attribute
    WHERE attrelid = CAST(:table_name AS regclass)
      AND attname = :column_name
      AND NOT attisdropped
""")


async def ensure_vector_column(
    session: AsyncSession,
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
