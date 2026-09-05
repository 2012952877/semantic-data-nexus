"""Opt-in public catalog composition over the reviewed identity and runtime boundaries."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import psycopg
import pyarrow as pa
from pydantic import ValidationError
from query_runtime.operators import ResourceLimits
from semantic_api.catalog_v1.catalog import fingerprint
from semantic_api.catalog_v1.clarification import PostgresClarifications
from semantic_api.catalog_v1.compiler import CatalogCompiler
from semantic_api.catalog_v1.guard import PublishedCatalogGuardAdapter
from semantic_api.catalog_v1.guarded_compiler import GuardedCatalogCompiler
from semantic_api.catalog_v1.guarded_store import GuardedClarifications
from semantic_api.catalog_v1.models import (
    CatalogCompileRequest,
    Compilation,
    CompilerFailure,
    Select,
)
from semantic_api.catalog_v1.provider import CatalogHTTPProvider
from semantic_api.catalog_v1.trust import owner_for
from semantic_api.catalog_v1.validator import CORE_CAPABILITIES
from semantic_api.models import ProviderSelection
from semantic_api.provider_config import ProviderSettings

from semantic_backend.auth_context import AccessDenied, TrustedContext
from semantic_backend.authorization import PostgresAuthorization
from semantic_backend.catalog_authorization import CatalogAuthorization
from semantic_backend.catalog_compilation import execute_catalog
from semantic_backend.catalog_configuration import CatalogRegistry
from semantic_backend.catalog_models import (
    CatalogAnswerRequest,
    CatalogAnswerResponse,
    CatalogQueryResponse,
)
from semantic_backend.models import ColumnFormat, ResultColumn, ResultSet, ScalarType
from semantic_backend.service import OrchestrationService

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
RUNTIME_VERSION: Literal["query-runtime/v1"] = "query-runtime/v1"


class PublicResultFailure(CompilerFailure):
    pass


def public_result(table: pa.Table, compilation: Compilation) -> ResultSet:
    assert compilation.graph is not None
    columns = []
    for index, field in enumerate(table.schema):
        kind = (
            ScalarType.INTEGER
            if compilation.graph.result_schema[index].data_type == "integer"
            else OrchestrationService._scalar_type(str(field.type))
        )
        display = (
            ColumnFormat.TIMESTAMP
            if kind is ScalarType.TIMESTAMP
            else ColumnFormat.DATE
            if kind is ScalarType.DATE
            else ColumnFormat.NUMBER
            if kind in {ScalarType.INTEGER, ScalarType.FLOAT, ScalarType.DECIMAL}
            else ColumnFormat.TEXT
        )
        columns.append(
            ResultColumn(
                key=field.name,
                label=field.name,
                data_type=kind,
                format=display,
                nullable=field.nullable,
            )
        )
    rows = []
    for raw in table.to_pylist():
        row = []
        for column in columns:
            value = raw[column.key]
            if column.data_type is ScalarType.INTEGER and value is not None:
                if (
                    isinstance(value, Decimal)
                    and value.is_finite()
                    and value == value.to_integral_value()
                ):
                    value = int(value)
                if type(value) is not int:
                    raise PublicResultFailure("RESULT_INTEGER_TYPE")
                if abs(value) > 9_007_199_254_740_991:
                    raise PublicResultFailure("RESULT_INTEGER_OUT_OF_RANGE")
            row.append(OrchestrationService._json_scalar(value, column.data_type))
        rows.append(row)
    try:
        return ResultSet(columns=columns, rows=rows, row_count=table.num_rows, truncated=False)
    except ValidationError:
        raise PublicResultFailure("RESULT_SCALAR_UNREPRESENTABLE") from None


class CatalogQueryService:
    def __init__(
        self,
        *,
        registry: CatalogRegistry,
        provider: CatalogHTTPProvider,
        authority: PostgresAuthorization,
        database: str,
    ) -> None:
        self.registry, self.provider, self.database = registry, provider, database
        self.authorization = CatalogAuthorization(authority)
        self.store = GuardedClarifications()
        self.core = CatalogCompiler(
            catalogs=registry.catalogs,
            authorization=self.authorization,
            provider=provider,
            clarifications=PostgresClarifications(database),
            capabilities=CORE_CAPABILITIES,
        )
        self.compiler = GuardedCatalogCompiler(
            core=self.core,
            guard=self.authorization,
            catalogs=PublishedCatalogGuardAdapter(registry.catalogs),
            store=self.store,
        )
        self.limits = ResourceLimits(
            max_rows=1000,
            max_bytes=4 * 1024 * 1024,
            memory_limit_bytes=64 * 1024 * 1024,
            node_timeout_seconds=5,
        )
        self._tasks: set[asyncio.Task[object]] = set()

    @classmethod
    def from_environment(cls, authority: PostgresAuthorization) -> CatalogQueryService | None:
        path = os.environ.get("SEMANTIC_NEXUS_CATALOG_CONFIG")
        if not path:
            return None
        if os.environ.get("SEMANTIC_NEXUS_AUTH_MODE", "service") != "service":
            raise ValueError("Catalog queries require verified service authentication")
        registry = CatalogRegistry.load(Path(path))
        settings = ProviderSettings.from_environment(
            {
                key.replace("SEMANTIC_CATALOG_PROVIDER_", "SEMANTIC_COMPILER_", 1): value
                for key, value in os.environ.items()
                if key.startswith("SEMANTIC_CATALOG_PROVIDER_")
            }
        )
        if settings.mode is not ProviderSelection.OPENAI_COMPATIBLE:
            raise ValueError(
                "Catalog queries require a reviewed structured provider; no fixture fallback"
            )
        return cls(
            registry=registry,
            provider=CatalogHTTPProvider(settings, os.environ.get(settings.credential_env, "")),
            authority=authority,
            database=os.environ.get("SEMANTIC_NEXUS_IDENTITY_POSTGRES", ""),
        )

    async def initialize(self) -> None:
        # Setup-only DDL; request transitions below exclusively use guard-owned connections.
        async with await psycopg.AsyncConnection.connect(self.database) as connection:
            async with connection.transaction():
                await self.store.initialize(connection)
                await connection.execute("""
                CREATE TABLE IF NOT EXISTS compiler_catalog_results_v1 (
                    record_id text NOT NULL
                        REFERENCES compiler_clarifications_v1(id) ON DELETE CASCADE,
                    compilation_sha text NOT NULL,
                    run_id text NOT NULL UNIQUE,
                    policy_sha text NOT NULL,
                    state text NOT NULL,
                    deadline_at timestamptz NOT NULL,
                    response text CHECK (octet_length(response) <= 8388608),
                    failure_code text,
                    PRIMARY KEY(record_id,compilation_sha)
                )
                """)
                await connection.execute("""
                    ALTER TABLE compiler_catalog_results_v1
                    ADD COLUMN IF NOT EXISTS failure_code text
                """)
                await connection.execute("""
                    ALTER TABLE compiler_catalog_results_v1
                    DROP CONSTRAINT IF EXISTS compiler_catalog_results_v1_state_check
                """)
                await connection.execute("""
                    ALTER TABLE compiler_catalog_results_v1
                    ADD CONSTRAINT compiler_catalog_results_v1_state_check
                    CHECK (state IN ('in_flight','completed','outcome_unknown','failed'))
                """)

    async def shutdown(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.provider.aclose()

    async def query(
        self, request: CatalogCompileRequest, context: TrustedContext
    ) -> CatalogQueryResponse:
        return await self._run(request, context, None, None)

    async def answer(
        self, identifier: str, answer: CatalogAnswerRequest, context: TrustedContext
    ) -> CatalogAnswerResponse:
        deadline = asyncio.get_running_loop().time() + 20
        owner = owner_for(context, answer.catalog)
        async with self.authorization.guard(
            context, answer.catalog, "compiler:query", deadline=deadline
        ) as decision:
            entry = self.registry.entry(answer.catalog)
            stored = await self.store.lock(decision.connection, identifier, owner)
            self.compiler._fresh(stored, context, entry.document, self.compiler._access(decision))
            request = stored.record.request
            if answer.request_id != request.request_id:
                raise CompilerFailure("CLARIFICATION_REQUEST_MISMATCH")
        outcome = await self._run(request, context, identifier, answer, deadline=deadline)
        return CatalogAnswerResponse(
            request_id=request.request_id,
            clarification_id=identifier,
            revision=answer.revision,
            choice_id=answer.choice_id,
            outcome=outcome,
        )

    async def _run(
        self,
        request: CatalogCompileRequest,
        context: TrustedContext,
        identifier: str | None,
        answer: CatalogAnswerRequest | None,
        *,
        deadline: float | None = None,
    ) -> CatalogQueryResponse:
        if not request.question.strip():
            raise CompilerFailure("QUESTION_REQUIRED")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Catalog query requires a task")
        limit = deadline or asyncio.get_running_loop().time() + 20
        stopped = asyncio.Event()
        denied = asyncio.Event()
        watcher: asyncio.Task[None] | None = None
        self._tasks.add(task)
        try:
            async with asyncio.timeout_at(limit):
                async with self.authorization.guard(
                    context, request.catalog, "compiler:query", deadline=limit
                ) as decision:
                    initial_access = self.compiler._access(decision)
                watcher = asyncio.create_task(
                    self._watch(
                        context,
                        request,
                        fingerprint({k: sorted(v) for k, v in initial_access.model_dump().items()}),
                        task,
                        stopped,
                        denied,
                        limit,
                    )
                )
                if answer is None:
                    compilation = await self.compiler.compile(
                        request, context=context, deadline=limit
                    )
                else:
                    assert identifier is not None
                    compilation = await self.compiler.resume(
                        identifier,
                        answer.choice_id,
                        revision=answer.revision,
                        context=context,
                        pin=answer.catalog,
                        deadline=limit,
                    )
                if compilation.status != "compiled":
                    return CatalogQueryResponse(
                        request_id=request.request_id,
                        compilation=compilation,
                        status="clarification_required"
                        if compilation.status == "clarification"
                        else "blocked",
                    )
                return await self._execute(request, compilation, context, limit)
        except asyncio.CancelledError:
            if denied.is_set():
                raise AccessDenied("Current catalog access is not authorized.") from None
            raise
        finally:
            stopped.set()
            self._tasks.discard(task)
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                failure = None if watcher.cancelled() else watcher.exception()
                if failure is not None:
                    raise failure

    async def _watch(
        self,
        context: TrustedContext,
        request: CatalogCompileRequest,
        access_hash: str,
        task: asyncio.Task[object],
        stopped: asyncio.Event,
        denied: asyncio.Event,
        deadline: float,
    ) -> None:
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), 0.25)
                return
            except TimeoutError:
                pass
            try:
                async with self.authorization.guard(
                    context, request.catalog, "compiler:query", deadline=deadline
                ) as decision:
                    access = self.compiler._access(decision)
                    if (
                        fingerprint({k: sorted(v) for k, v in access.model_dump().items()})
                        != access_hash
                    ):
                        raise AccessDenied("Catalog grants changed.")
            except (AccessDenied, CompilerFailure, psycopg.Error, TimeoutError):
                denied.set()
                task.cancel()
                return

    async def _execute(
        self,
        request: CatalogCompileRequest,
        compilation: Compilation,
        context: TrustedContext,
        deadline: float,
    ) -> CatalogQueryResponse:
        async with asyncio.timeout_at(deadline) as budget:
            return await self._execute_with_budget(request, compilation, context, deadline, budget)

    async def _execute_with_budget(
        self,
        request: CatalogCompileRequest,
        compilation: Compilation,
        context: TrustedContext,
        deadline: float,
        budget: asyncio.Timeout,
    ) -> CatalogQueryResponse:
        assert compilation.graph is not None
        owner = owner_for(context, request.catalog)
        entry = self.registry.entry(request.catalog)
        compilation_sha = fingerprint(compilation.model_dump(mode="json"))
        policy_sha = fingerprint(
            {
                "binding": entry.bindings.content_sha256,
                "runtime": RUNTIME_VERSION,
                "rows": self.limits.max_rows,
                "bytes": self.limits.max_bytes,
            }
        )
        outcome: CatalogQueryResponse | None = None
        rejection: str | None = None
        async with self.authorization.guard(
            context, request.catalog, "compiler:query", deadline=deadline
        ) as decision:
            stored = await self.store.resolve(decision.connection, request.request_id, owner)
            self.compiler._fresh(stored, context, entry.document, self.compiler._access(decision))
            deadline = await self.compiler._record_deadline(
                decision.connection, stored.record, context, deadline
            )
            budget.reschedule(deadline)
            record_id = stored.record.id
            row = await (
                await decision.connection.execute(
                    """SELECT run_id,policy_sha,state,response,
                              deadline_at>clock_timestamp(),failure_code
                       FROM compiler_catalog_results_v1
                       WHERE record_id=%s AND compilation_sha=%s FOR UPDATE""",
                    (record_id, compilation_sha),
                )
            ).fetchone()
            if row is not None:
                if row[1] != policy_sha:
                    raise CompilerFailure("RUNTIME_POLICY_CHANGED")
                if row[2] == "completed" and isinstance(row[3], str):
                    outcome = CatalogQueryResponse.model_validate_json(row[3])
                    if (
                        outcome.compilation != compilation
                        or outcome.request_id != request.request_id
                        or outcome.status != "succeeded"
                        or outcome.result is None
                        or outcome.run_id != row[0]
                    ):
                        raise CompilerFailure("RUNTIME_RESULT_INVALID")
                elif row[2] == "failed":
                    if row[5] not in {
                        "RESULT_INTEGER_TYPE",
                        "RESULT_INTEGER_OUT_OF_RANGE",
                        "RESULT_SCALAR_UNREPRESENTABLE",
                    }:
                        raise CompilerFailure("RUNTIME_RESULT_INVALID")
                    assert isinstance(row[5], str)
                    rejection = row[5]
                else:
                    unknown = row[2] == "outcome_unknown" or not row[4]
                    if unknown:
                        await decision.connection.execute(
                            """UPDATE compiler_catalog_results_v1 SET state='outcome_unknown'
                               WHERE record_id=%s AND compilation_sha=%s""",
                            (record_id, compilation_sha),
                        )
                    rejection = "RUNTIME_OUTCOME_UNKNOWN" if unknown else "RUNTIME_IN_PROGRESS"
                if not isinstance(row[0], str):
                    raise CompilerFailure("RUNTIME_RESULT_INVALID")
                run_id = row[0]
            else:
                run_id = (
                    "run_" + fingerprint({"record": record_id, "compile": compilation_sha})[:32]
                )
                now = await self.store.now(decision.connection)
                expiry = min(
                    stored.record.expires_at,
                    context.authentication.expires_at,
                    now + timedelta(seconds=max(0, deadline - asyncio.get_running_loop().time())),
                )
                await decision.connection.execute(
                    """INSERT INTO compiler_catalog_results_v1
                       (record_id,compilation_sha,run_id,policy_sha,state,deadline_at)
                       VALUES (%s,%s,%s,%s,'in_flight',%s)""",
                    (record_id, compilation_sha, run_id, policy_sha, expiry),
                )
        if rejection is not None:
            raise CompilerFailure(rejection)
        if outcome is not None:
            return outcome
        entities = {
            n.operation.entity_id
            for n in compilation.graph.nodes
            if isinstance(n.operation, Select)
        }
        execution = await execute_catalog(
            request,
            compilation,
            compiler=self.core,
            context=context,
            bindings=entry.bindings,
            resolver=self.registry.resolver(request.catalog, entities, self.limits),
            run_id=run_id,
            deadline=deadline,
            limits=self.limits,
            runtime_version=RUNTIME_VERSION,
        )
        try:
            result = public_result(execution.table, compilation)
        except PublicResultFailure as error:
            async with self.authorization.guard(
                context, request.catalog, "compiler:query", deadline=deadline
            ) as decision:
                stored = await self.store.lock(decision.connection, record_id, owner)
                self.compiler._fresh(
                    stored, context, entry.document, self.compiler._access(decision)
                )
                cursor = await decision.connection.execute(
                    """UPDATE compiler_catalog_results_v1 SET state='failed',failure_code=%s
                       WHERE record_id=%s AND compilation_sha=%s AND policy_sha=%s
                         AND state='in_flight' AND deadline_at>clock_timestamp()""",
                    (error.code, record_id, compilation_sha, policy_sha),
                )
                if cursor.rowcount != 1:
                    raise CompilerFailure("RUNTIME_FENCED") from None
            raise
        response = CatalogQueryResponse(
            request_id=request.request_id,
            status="succeeded",
            compilation=compilation,
            run_id=run_id,
            result=result,
            provenance={
                **execution.metadata,
                "resolver": "catalog-synthetic",
                "operators": ",".join(n.operation.value for n in execution.plan.nodes),
            },
        )
        payload = response.model_dump_json()
        if len(payload.encode()) > MAX_RESPONSE_BYTES:
            raise CompilerFailure("RUNTIME_RESPONSE_LIMIT")
        async with self.authorization.guard(
            context, request.catalog, "compiler:query", deadline=deadline
        ) as decision:
            stored = await self.store.lock(decision.connection, record_id, owner)
            self.compiler._fresh(stored, context, entry.document, self.compiler._access(decision))
            cursor = await decision.connection.execute(
                """UPDATE compiler_catalog_results_v1 SET state='completed',response=%s
                   WHERE record_id=%s AND compilation_sha=%s AND policy_sha=%s
                         AND state='in_flight' AND deadline_at>clock_timestamp()""",
                (payload, record_id, compilation_sha, policy_sha),
            )
            if cursor.rowcount != 1:
                raise CompilerFailure("RUNTIME_FENCED")
        return response
