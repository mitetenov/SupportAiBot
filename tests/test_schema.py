"""Regression tests for runtime reconciliation of pre-existing databases."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.storage import schema


class _Result:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _Connection:
    def __init__(self) -> None:
        self.ddl: list[str] = []

    async def execute(self, statement: Any, params: dict[str, str] | None = None) -> _Result:
        params = params or {}
        if statement is schema._TABLE_EXISTS_SQL:
            exists = params.get("table_name") in {"bedolaga_ticket_state", "topic_mappings"}
            return _Result((1,) if exists else None)
        if statement is schema._COLUMN_TYPE_SQL:
            if params.get("column_name") in {"last_human_reply_at", "active_ticket_id"}:
                return _Result(None)
            return _Result(("bigint",))

        self.ddl.append(str(statement))
        return _Result(None)


class _Engine:
    def __init__(self) -> None:
        self.connection = _Connection()

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[_Connection]:
        yield self.connection


async def test_human_reply_timestamp_is_added_nullable_without_a_fake_now() -> None:
    """Old rows mean "no human reply" until a real one is observed.

    Adding this field as NOT NULL DEFAULT now() would both break normal writes
    of ``None`` and make every existing ticket look freshly owned by a human.
    """
    engine = _Engine()

    changes = await schema.sync_legacy_schema(engine)  # type: ignore[arg-type]

    matching = [ddl for ddl in engine.connection.ddl if "last_human_reply_at" in ddl]
    assert matching == [
        "ALTER TABLE bedolaga_ticket_state ADD COLUMN last_human_reply_at TIMESTAMPTZ NULL"
    ]
    assert "bedolaga_ticket_state.last_human_reply_at: added" in changes
    assert "NOT NULL" not in matching[0]
    assert "DEFAULT" not in matching[0]


async def test_active_ticket_pointer_is_added_nullable() -> None:
    engine = _Engine()

    changes = await schema.sync_legacy_schema(engine)  # type: ignore[arg-type]

    matching = [ddl for ddl in engine.connection.ddl if "active_ticket_id" in ddl]
    assert matching == ["ALTER TABLE topic_mappings ADD COLUMN active_ticket_id BIGINT NULL"]
    assert "topic_mappings.active_ticket_id: added" in changes
