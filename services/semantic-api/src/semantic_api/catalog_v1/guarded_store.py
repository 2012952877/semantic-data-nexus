"""Connection-bound storage. The injected authorization guard owns commit/rollback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus
from pydantic import AwareDatetime, Field, ValidationError

from semantic_api.catalog_v1.catalog import fingerprint
from semantic_api.catalog_v1.clarification import (
    ClarificationRecord,
    initialize_clarification_schema,
)
from semantic_api.catalog_v1.models import Compilation, CompilerFailure, Frozen
from semantic_api.catalog_v1.trust import Owner

Connection = AsyncConnection[tuple[object, ...]]


class Reservation(Frozen):
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    provider_calls: int = Field(ge=0, le=2, strict=True)


class Attempt(Frozen):
    record_id: str
    step: int
    fence: str
    action_hash: str
    state: Literal["in_flight", "completed", "outcome_unknown"]
    claim_generation: int
    created_at: AwareDatetime
    deadline_at: AwareDatetime | None
    lease_until: AwareDatetime | None
    reserved_input_tokens: int | None
    reserved_output_tokens: int | None
    reserved_calls: int | None
    used_input_tokens: int | None
    used_output_tokens: int | None
    completed_at: AwareDatetime | None
    origin: Literal["guarded", "legacy_unknown"]


@dataclass(frozen=True)
class StoredRecord:
    record: ClarificationRecord
    generation: int
    guarded_version: int


def action_hash(record: ClarificationRecord, step: int, choice: str | None) -> str:
    return fingerprint(
        {
            "request": record.request.model_dump(mode="json"),
            "step": step,
            "choice": choice,
        }
    )


def require_transaction(connection: Connection) -> None:
    if connection.info.transaction_status is not TransactionStatus.INTRANS:
        raise CompilerFailure("GUARD_TRANSACTION_REQUIRED")


class GuardedClarifications:
    async def initialize(self, connection: Connection) -> None:
        require_transaction(connection)
        await initialize_clarification_schema(connection)
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS compiler_clarification_attempts_v1 (
                record_id text NOT NULL REFERENCES compiler_clarifications_v1(id) ON DELETE CASCADE,
                step integer NOT NULL CHECK (step >= 0),
                fence text NOT NULL UNIQUE,
                action_hash text NOT NULL,
                state text NOT NULL CHECK (state IN ('in_flight','completed','outcome_unknown')),
                claim_generation bigint NOT NULL,
                created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                deadline_at timestamptz,
                lease_until timestamptz,
                reserved_input_tokens bigint CHECK (reserved_input_tokens >= 0),
                reserved_output_tokens bigint CHECK (reserved_output_tokens >= 0),
                reserved_calls integer CHECK (reserved_calls >= 0 AND reserved_calls <= 2),
                used_input_tokens bigint CHECK (used_input_tokens >= 0),
                used_output_tokens bigint CHECK (used_output_tokens >= 0),
                completed_at timestamptz,
                origin text NOT NULL CHECK (origin IN ('guarded','legacy_unknown')),
                PRIMARY KEY (record_id, step),
                CHECK ((origin = 'legacy_unknown' AND state = 'outcome_unknown')
                    OR (deadline_at IS NOT NULL AND lease_until IS NOT NULL
                        AND lease_until <= deadline_at AND reserved_input_tokens IS NOT NULL
                        AND reserved_output_tokens IS NOT NULL AND reserved_calls IS NOT NULL))
            )
        """)

    async def now(self, connection: Connection) -> datetime:
        require_transaction(connection)
        cursor = await connection.execute("SELECT clock_timestamp()")
        row = await cursor.fetchone()
        if row is None or not isinstance(row[0], datetime) or row[0].tzinfo is None:
            raise CompilerFailure("CLARIFICATION_STORE_INVALID")
        return row[0]

    async def create(self, connection: Connection, record: ClarificationRecord) -> StoredRecord:
        require_transaction(connection)
        owner_hash = fingerprint(record.owner.model_dump(mode="json"))
        request_hash = fingerprint(record.request.model_dump(mode="json"))
        await connection.execute(
            """INSERT INTO compiler_clarifications_v1
               (id,owner_hash,request_id,request_hash,expires_at,payload,guarded_version)
               VALUES (%s,%s,%s,%s,%s,%s,1)
               ON CONFLICT (owner_hash,request_id) DO NOTHING""",
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
            """SELECT id,request_hash FROM compiler_clarifications_v1
               WHERE owner_hash=%s AND request_id=%s FOR UPDATE""",
            (owner_hash, record.request.request_id),
        )
        row = await cursor.fetchone()
        if row is None or not isinstance(row[0], str):
            raise CompilerFailure("CLARIFICATION_STORE_INVALID")
        if row[1] != request_hash:
            raise CompilerFailure("IDEMPOTENCY_CONFLICT")
        return await self.lock(connection, row[0], record.owner)

    async def lock(self, connection: Connection, identifier: str, owner: Owner) -> StoredRecord:
        require_transaction(connection)
        cursor = await connection.execute(
            """SELECT payload,guarded_version,generation,expires_at
               FROM compiler_clarifications_v1 WHERE id=%s AND owner_hash=%s FOR UPDATE""",
            (identifier, fingerprint(owner.model_dump(mode="json"))),
        )
        row = await cursor.fetchone()
        if row is None:
            raise CompilerFailure("CLARIFICATION_NOT_AVAILABLE")
        if (
            not isinstance(row[0], str)
            or type(row[1]) is not int
            or type(row[2]) is not int
            or not isinstance(row[3], datetime)
        ):
            raise CompilerFailure("CLARIFICATION_STORE_INVALID")
        if row[3] <= await self.now(connection):
            raise CompilerFailure("CLARIFICATION_EXPIRED")
        try:
            record = ClarificationRecord.model_validate_json(row[0])
        except ValidationError:
            raise CompilerFailure("CLARIFICATION_STORE_INVALID") from None
        if record.id != identifier or record.owner != owner or record.expires_at != row[3]:
            raise CompilerFailure("CLARIFICATION_STORE_INVALID")
        if row[1] not in (0, 1):
            raise CompilerFailure("CLARIFICATION_VERSION_UNSUPPORTED")
        return StoredRecord(record, row[2], row[1])

    async def adopt(self, connection: Connection, stored: StoredRecord) -> StoredRecord:
        require_transaction(connection)
        await connection.execute(
            "UPDATE compiler_clarifications_v1 SET guarded_version=1 WHERE id=%s",
            (stored.record.id,),
        )
        return StoredRecord(stored.record, stored.generation, 1)

    async def attempt(self, connection: Connection, identifier: str, step: int) -> Attempt | None:
        require_transaction(connection)
        cursor = await connection.execute(
            """SELECT row_to_json(a)::text FROM compiler_clarification_attempts_v1 a
               WHERE record_id=%s AND step=%s FOR UPDATE""",
            (identifier, step),
        )
        row = await cursor.fetchone()
        return self._attempt(row)

    async def active(self, connection: Connection, identifier: str) -> Attempt | None:
        require_transaction(connection)
        cursor = await connection.execute(
            """SELECT row_to_json(a)::text FROM compiler_clarification_attempts_v1 a
               WHERE record_id=%s AND state IN ('in_flight','outcome_unknown')
               ORDER BY step DESC LIMIT 1 FOR UPDATE""",
            (identifier,),
        )
        return self._attempt(await cursor.fetchone())

    @staticmethod
    def _attempt(row: tuple[object, ...] | None) -> Attempt | None:
        if row is None:
            return None
        if not isinstance(row[0], str):
            raise CompilerFailure("CLARIFICATION_STORE_INVALID")
        try:
            return Attempt.model_validate_json(row[0])
        except ValidationError:
            raise CompilerFailure("CLARIFICATION_STORE_INVALID") from None

    async def claim(
        self,
        connection: Connection,
        stored: StoredRecord,
        *,
        step: int,
        choice: str | None,
        deadline_at: datetime | None,
        reservation: Reservation | None,
    ) -> Attempt:
        require_transaction(connection)
        cursor = await connection.execute(
            """UPDATE compiler_clarifications_v1 SET generation=generation+1
               WHERE id=%s AND generation=%s AND guarded_version=1 RETURNING generation""",
            (stored.record.id, stored.generation),
        )
        row = await cursor.fetchone()
        if row is None or type(row[0]) is not int:
            raise CompilerFailure("ATTEMPT_FENCED")
        legacy = reservation is None
        await connection.execute(
            """INSERT INTO compiler_clarification_attempts_v1
               (record_id,step,fence,action_hash,state,claim_generation,deadline_at,lease_until,
                reserved_input_tokens,reserved_output_tokens,reserved_calls,origin)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                stored.record.id,
                step,
                uuid4().hex,
                action_hash(stored.record, step, choice),
                "outcome_unknown" if legacy else "in_flight",
                row[0],
                deadline_at,
                deadline_at,
                None if reservation is None else reservation.input_tokens,
                None if reservation is None else reservation.output_tokens,
                None if reservation is None else reservation.provider_calls,
                "legacy_unknown" if legacy else "guarded",
            ),
        )
        result = await self.attempt(connection, stored.record.id, step)
        assert result is not None
        return result

    async def mark_unknown(self, connection: Connection, attempt: Attempt) -> None:
        require_transaction(connection)
        await connection.execute(
            """UPDATE compiler_clarification_attempts_v1 SET state='outcome_unknown'
               WHERE record_id=%s AND step=%s AND fence=%s AND state='in_flight'""",
            (attempt.record_id, attempt.step, attempt.fence),
        )

    async def finish(
        self,
        connection: Connection,
        stored: StoredRecord,
        attempt: Attempt,
        updated: ClarificationRecord,
        response: Compilation,
    ) -> None:
        require_transaction(connection)
        if (
            attempt.reserved_calls is None
            or len(response.calls) > attempt.reserved_calls
            or any(
                value is not None and (value < 0 or limit is None or value > limit)
                for value, limit in (
                    (response.input_tokens, attempt.reserved_input_tokens),
                    (response.output_tokens, attempt.reserved_output_tokens),
                )
            )
        ):
            raise CompilerFailure("ATTEMPT_BUDGET")
        cursor = await connection.execute(
            """UPDATE compiler_clarifications_v1 SET payload=%s,generation=generation+1
               WHERE id=%s AND generation=%s AND guarded_version=1
                     AND expires_at>clock_timestamp()""",
            (updated.model_dump_json(), stored.record.id, attempt.claim_generation),
        )
        if cursor.rowcount != 1:
            raise CompilerFailure("ATTEMPT_FENCED")
        cursor = await connection.execute(
            """UPDATE compiler_clarification_attempts_v1
               SET state='completed',used_input_tokens=%s,used_output_tokens=%s,
                   completed_at=clock_timestamp()
               WHERE record_id=%s AND step=%s AND fence=%s AND action_hash=%s
                     AND claim_generation=%s AND state='in_flight'
                     AND deadline_at>clock_timestamp() AND lease_until>clock_timestamp()""",
            (
                response.input_tokens,
                response.output_tokens,
                attempt.record_id,
                attempt.step,
                attempt.fence,
                attempt.action_hash,
                attempt.claim_generation,
            ),
        )
        if cursor.rowcount != 1:
            raise CompilerFailure("ATTEMPT_FENCED")
