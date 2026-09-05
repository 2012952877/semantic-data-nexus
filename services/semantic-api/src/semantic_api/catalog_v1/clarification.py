from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import AwareDatetime

from semantic_api.catalog_v1.models import (
    CatalogCompileRequest,
    Compilation,
    CompilerContext,
    CompilerFailure,
    Frozen,
    Resolution,
)
from semantic_api.catalog_v1.trust import Owner

if TYPE_CHECKING:
    from psycopg import AsyncConnection


class Answered(Frozen):
    choice_id: str
    response: Compilation


class ClarificationRecord(Frozen):
    id: str
    owner: Owner
    request: CatalogCompileRequest
    context: CompilerContext
    authority_fingerprint: str
    expires_at: AwareDatetime
    answers: tuple[Resolution, ...] = ()
    history: tuple[Answered, ...] = ()
    current: Compilation | None = None


class LockedClarification(Protocol):
    record: ClarificationRecord

    async def save(self, record: ClarificationRecord) -> None: ...

    async def now(self) -> datetime: ...


class Clarifications(Protocol):
    async def create(self, record: ClarificationRecord) -> ClarificationRecord: ...

    def lock(
        self, clarification_id: str, owner: Owner
    ) -> AbstractAsyncContextManager[LockedClarification]: ...


class PostgresClarifications:
    """Dedicated transactional store. No control-plane or identity schema is modified."""

    def __init__(self, dsn: str) -> None:
        # The composition root resolves its own environment secret reference.
        self._dsn = dsn

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[AsyncConnection[tuple[Any, ...]]]:
        import psycopg

        try:
            async with await psycopg.AsyncConnection.connect(
                self._dsn, connect_timeout=5
            ) as connection:
                await connection.execute("SET LOCAL statement_timeout = '120s'")
                yield connection
        except psycopg.Error:
            raise CompilerFailure("CLARIFICATION_STORE_UNAVAILABLE") from None

    async def initialize(self) -> None:
        async with self._connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('compiler_clarifications_v1_schema'))"
            )
            await connection.execute("""
                CREATE TABLE IF NOT EXISTS compiler_clarifications_v1 (
                    id text PRIMARY KEY,
                    owner_hash text NOT NULL,
                    request_id text NOT NULL,
                    request_hash text NOT NULL,
                    expires_at timestamptz NOT NULL,
                    payload text NOT NULL CHECK (octet_length(payload) <= 1048576),
                    UNIQUE (owner_hash, request_id)
                )
            """)

    async def create(self, record: ClarificationRecord) -> ClarificationRecord:
        from semantic_api.catalog_v1.catalog import fingerprint

        owner_hash = fingerprint(record.owner.model_dump(mode="json"))
        request_hash = fingerprint(record.request.model_dump(mode="json"))
        async with self._connection() as connection:
            await connection.execute(
                """INSERT INTO compiler_clarifications_v1
                   (id, owner_hash, request_id, request_hash, expires_at, payload)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (owner_hash, request_id) DO NOTHING""",
                (
                    record.id,
                    owner_hash,
                    record.request.request_id,
                    request_hash,
                    record.expires_at,
                    record.model_dump_json(),
                ),
            )
            cursor = await connection.execute(
                """SELECT request_hash, payload, expires_at > clock_timestamp()
                   FROM compiler_clarifications_v1
                   WHERE owner_hash = %s AND request_id = %s FOR UPDATE""",
                (owner_hash, record.request.request_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise CompilerFailure("CLARIFICATION_NOT_AVAILABLE")
            if row[0] != request_hash:
                raise CompilerFailure("IDEMPOTENCY_CONFLICT")
            if not row[2]:
                raise CompilerFailure("CLARIFICATION_EXPIRED")
            stored = ClarificationRecord.model_validate_json(row[1])
            if (
                stored.authority_fingerprint != record.authority_fingerprint
                or stored.context != record.context
            ):
                raise CompilerFailure("CLARIFICATION_CONTEXT_CHANGED")
            return stored

    @asynccontextmanager
    async def lock(self, clarification_id: str, owner: Owner) -> AsyncIterator[LockedClarification]:
        from semantic_api.catalog_v1.catalog import fingerprint

        owner_hash = fingerprint(owner.model_dump(mode="json"))
        async with self._connection() as connection:
            # asyncio's encompassing deadline also bounds lock waits. No detached DB task.
            cursor = await connection.execute(
                """SELECT payload, expires_at > clock_timestamp()
                   FROM compiler_clarifications_v1 WHERE id = %s AND owner_hash = %s
                   FOR UPDATE""",
                (clarification_id, owner_hash),
            )
            row = await cursor.fetchone()
            if row is None:
                raise CompilerFailure("CLARIFICATION_NOT_AVAILABLE")
            if not row[1]:
                raise CompilerFailure("CLARIFICATION_EXPIRED")
            record = ClarificationRecord.model_validate_json(row[0])

            class Transaction:
                def __init__(self) -> None:
                    self.record = record

                async def now(self) -> datetime:
                    result = await connection.execute("SELECT clock_timestamp()")
                    value = await result.fetchone()
                    assert value is not None
                    current: datetime = value[0]
                    return current

                async def save(self, updated: ClarificationRecord) -> None:
                    if updated.id != record.id or updated.owner != owner:
                        raise CompilerFailure("CLARIFICATION_NOT_AVAILABLE")
                    if await self.now() >= record.expires_at:
                        raise CompilerFailure("CLARIFICATION_EXPIRED")
                    await connection.execute(
                        """UPDATE compiler_clarifications_v1 SET payload = %s
                           WHERE id = %s AND owner_hash = %s""",
                        (updated.model_dump_json(), record.id, owner_hash),
                    )
                    self.record = updated

            yield Transaction()
