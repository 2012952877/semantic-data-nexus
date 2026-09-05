from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa
from psycopg import Error as PostgresError
from query_runtime.coordinator import QueryCoordinator, RunOutcome
from query_runtime.domain import (
    AggregateFunction,
    CapabilityCatalog,
    DiagnosticEvent,
    ExecutionState,
    PhysicalNode,
    PhysicalPlan,
)
from query_runtime.domain import (
    OperatorKind as RuntimeOperatorKind,
)
from query_runtime.errors import RuntimeFailure
from query_runtime.operators import ResourceLimits
from query_runtime.planner import CapabilityPlanner
from query_runtime.resolver import SourceResolver
from query_runtime.result_store import InlineResultStore
from semantic_api.compiler import SemanticCompiler
from semantic_api.models import (
    AggregateParameters,
    CompileRequest,
    CompileResponse,
    CompileStatus,
    InitializeRequest,
    Operator,
    ResolvedTermKind,
)

from semantic_backend.adapter import AdapterFailure, CompilerRuntimeAdapter
from semantic_backend.auth_context import AccessDenied, TrustedContext, legacy_development
from semantic_backend.authorization import MembershipAuthority, PostgresAuthorization
from semantic_backend.models import (
    MAX_SERIALIZED_DETAIL_BYTES,
    ColumnFormat,
    CommittedManifest,
    DetailDiagnostic,
    DiagnosticScope,
    DiagnosticSeverity,
    DiagnosticSummary,
    LineageDetail,
    LineageEdgeDetail,
    LineageNodeDetail,
    LineageNodeKind,
    LineageParameter,
    LineageRelation,
    NodeSummary,
    OperatorKind,
    PhysicalNodeDetail,
    ResultColumn,
    ResultSet,
    ResultStorage,
    RunDetail,
    RunState,
    RunStatus,
    ScalarType,
    SqgFilter,
    SqgSummary,
    StageSummary,
    StartRunRequest,
    TokenUsage,
    serialized_detail_size,
)
from semantic_backend.repository import InMemoryRunRepository, RunRecord, RunRepository
from semantic_backend.resolver_factory import resolver_from_environment

_STAGES = ("Initialize", "Compile", "Optimize", "Execute", "Generate")
_MAX_RESULT_ROWS = 1_000
_MAX_RESULT_BYTES = 4 * 1024 * 1024
_RUN_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class IntegratedRunArtifact:
    detail: RunDetail
    compile_response: CompileResponse
    physical_plan: PhysicalPlan
    connector_provenance: tuple[dict[str, str], ...] = ()
    member_normalization: dict[str, str] | None = None


class OrchestrationService:
    def __init__(
        self,
        *,
        compiler: SemanticCompiler | None = None,
        adapter: CompilerRuntimeAdapter | None = None,
        repository: RunRepository | None = None,
        resolver: SourceResolver | None = None,
        authority: MembershipAuthority | None = None,
    ) -> None:
        self._legacy = legacy_development()
        self.authority = authority or (
            None
            if self._legacy
            else PostgresAuthorization(os.environ.get("SEMANTIC_NEXUS_IDENTITY_POSTGRES", ""))
        )
        self.compiler = compiler or SemanticCompiler.default()
        self.adapter = adapter or CompilerRuntimeAdapter()
        self.repository = repository or InMemoryRunRepository()
        if resolver is None:
            resolver = resolver_from_environment(self.adapter)
        self.resolver = resolver
        self._semaphore = asyncio.Semaphore(4)

    def _catalog(self) -> CapabilityCatalog:
        source = self.adapter.mapping.source
        return CapabilityCatalog(
            source_alias=source.alias,
            source_type=source.source_type,
            operator_kinds=frozenset(
                {
                    RuntimeOperatorKind.SELECT,
                    RuntimeOperatorKind.FILTER,
                    RuntimeOperatorKind.AGGREGATE,
                    RuntimeOperatorKind.SORT,
                }
            ),
            aggregate_functions=frozenset(AggregateFunction),
            max_rows=100_000,
            max_bytes=32 * 1024 * 1024,
        )

    async def ready(self) -> bool:
        if isinstance(self.authority, PostgresAuthorization) and not await self.authority.ready():
            return False
        return self.compiler.ready and await self.resolver.health()

    async def start(
        self, request: StartRunRequest, *, context: TrustedContext | None = None
    ) -> RunStatus:
        await self._authorize_request(context, "run.contributor")
        if context is not None and request.requested_by != context.principal.principal_id:
            raise AccessDenied("Run principal must match the verified context.")
        now = datetime.now(UTC)
        status = RunStatus(
            run_id=request.run_id,
            state=RunState.STARTING,
            started_at=now,
            stages=[
                StageSummary(
                    stage_id=name.lower(),
                    name=name,
                    state=RunState.STARTING if index == 0 else RunState.QUEUED,
                    started_at=now if index == 0 else None,
                    nodes=[
                        NodeSummary(
                            node_id=f"{name.lower()}-1",
                            kind="orchestration",
                            state=RunState.STARTING if index == 0 else RunState.QUEUED,
                            started_at=now if index == 0 else None,
                        )
                    ],
                )
                for index, name in enumerate(_STAGES)
            ],
        )
        record, created = await self.repository.create(request, status, context=context)
        if created:
            record.deadline = asyncio.get_running_loop().time() + _RUN_TIMEOUT_SECONDS
            record.task = asyncio.create_task(
                self._run(record),
                name=f"semantic-backend-{request.run_id}",
            )
            if record.trusted_context is not None:
                record.revocation_task = asyncio.create_task(self._watch_authorization(record))
        return record.status.model_copy(deep=True)

    async def get_status(self, run_id: str, *, context: TrustedContext | None = None) -> RunStatus:
        await self._authorize_request(context, "run.reader")
        record = await self.repository.get(run_id, context=context)
        async with record.lock:
            return record.status.model_copy(deep=True)

    async def get_detail(self, run_id: str, *, context: TrustedContext | None = None) -> RunDetail:
        await self._authorize_request(context, "run.reader")
        record = await self.repository.get(run_id, context=context)
        async with record.lock:
            return record.detail.model_copy(deep=True)

    async def get_integrated_artifact(
        self, run_id: str, *, context: TrustedContext | None = None
    ) -> IntegratedRunArtifact:
        await self._authorize_request(context, "run.reader")
        record = await self.repository.get(run_id, context=context)
        async with record.lock:
            if (
                record.status.state is not RunState.SUCCEEDED
                or record.compile_response is None
                or record.physical_plan is None
            ):
                raise ValueError("integrated artifacts require a succeeded run")
            return IntegratedRunArtifact(
                detail=record.detail.model_copy(deep=True),
                compile_response=record.compile_response.model_copy(deep=True),
                physical_plan=record.physical_plan.model_copy(deep=True),
                connector_provenance=self._connector_provenance(run_id),
                member_normalization={
                    term.machine_id: self.adapter.mapping.members[term.machine_id]
                    for term in record.compile_response.resolved_terms
                    if term.kind is ResolvedTermKind.MEMBER
                },
            )

    async def cancel(self, run_id: str, *, context: TrustedContext | None = None) -> RunStatus:
        await self._authorize_request(context, "run.contributor")
        record = await self.repository.get(run_id, context=context)
        return await self._cancel_record(record)

    async def _cancel_record(self, record: RunRecord) -> RunStatus:
        run_id = record.request.run_id
        async with record.lock:
            if record.status.state.terminal:
                return record.status.model_copy(deep=True)
            record.cancel_requested = True
            task = record.task
            coordinator = None if record.terminal_cleanup_started else record.coordinator
        accepted = False
        finishing = False
        if coordinator is not None:
            accepted = await coordinator.cancel(run_id)
            if accepted:
                async with record.lock:
                    record.cancel_accepted = True
            if not accepted:
                async with record.lock:
                    accepted = record.cancel_accepted
                events = () if accepted else await coordinator.event_store.list(run_id)
                if events:
                    finishing = True
                    await self._apply_runtime_events(record, events)
                    async with record.lock:
                        record.terminal_observed = True
        current = asyncio.current_task()
        async with record.lock:
            if (
                not accepted
                and not finishing
                and not record.terminal_cleanup_started
                and task is not None
                and task is not current
                and not task.done()
                and not task.cancelling()
            ):
                task.cancel()
        if task is not None and task is not current and not task.done():
            # A disconnected cancel caller must not cancel the run's bounded terminal cleanup.
            outcomes = await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            for outcome in outcomes:
                if isinstance(outcome, Exception):
                    raise outcome
        await self._finalize_cancelled(record)
        return record.status.model_copy(deep=True)

    async def shutdown(self) -> None:
        records = await self.repository._shutdown_records()
        current = asyncio.current_task()
        results = await asyncio.gather(
            *(
                self._cancel_record(record)
                for record in records
                if not record.status.state.terminal and record.task is not current
            ),
            return_exceptions=True,
        )
        for record in records:
            await self._stop_revocation_watch(record)
        close = getattr(self.resolver, "aclose", None)
        try:
            if close is not None:
                await close()
        finally:
            await self.compiler.aclose()
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise RuntimeError(
                "One or more run cancellations failed during shutdown."
            ) from failures[0]

    async def _run(self, record: RunRecord) -> None:
        request = record.request
        run_timeout = asyncio.timeout_at(record.deadline)
        try:
            async with run_timeout, self._semaphore:
                await self._ensure_active(record)
                await self._begin_stage(record, "Initialize")
                initialize_request = InitializeRequest(
                    question=request.question,
                    evaluation_clock=request.evaluation_clock,
                    evaluation_timezone=request.evaluation_timezone,
                    compilation_mode=request.compilation_mode,
                )
                initialization = self.compiler.initialize(initialize_request, request.trace_id)
                if initialization.status is not CompileStatus.SUCCEEDED:
                    code = (
                        initialization.diagnostics[0].code
                        if initialization.diagnostics
                        else "INITIALIZE_FAILED"
                    )
                    raise AdapterFailure(code, "The semantic request could not be initialized.")
                await self._complete_stage(record, "Initialize")

                await self._begin_stage(record, "Compile")
                compile_response = await self.compiler.compile(
                    CompileRequest.model_validate(initialize_request.model_dump()),
                    request.trace_id,
                    deadline=record.deadline,
                )
                if run_timeout.expired():
                    raise TimeoutError("The whole-run deadline has expired.")
                await self._ensure_active(record)
                async with record.lock:
                    record.compile_response = compile_response
                if compile_response.status is not CompileStatus.SUCCEEDED:
                    code = (
                        compile_response.diagnostics[-1].code
                        if compile_response.diagnostics
                        else "COMPILE_FAILED"
                    )
                    raise AdapterFailure(code, "The semantic request did not compile.")
                await self._set_compile_artifact(record, compile_response)
                await self._complete_stage(record, "Compile")

                await self._begin_stage(record, "Optimize")
                adapted = self.adapter.adapt(
                    compile_response,
                    run_id=request.run_id,
                    compilation_mode=request.compilation_mode,
                    source_type=getattr(
                        self.resolver,
                        "source_type",
                        self.adapter.mapping.source.source_type,
                    ),
                )
                plan = CapabilityPlanner(
                    binder=adapted.binder,
                    sources=adapted.sources,
                    capabilities=adapted.capabilities,
                ).plan(adapted.graph)
                await self._set_plan(record, plan)
                await self._complete_stage(record, "Optimize")

                await self._begin_stage(record, "Execute", plan=plan)
                store = InlineResultStore()
                coordinator = QueryCoordinator(
                    resolver=self.resolver,
                    result_store=store,
                    limits=ResourceLimits(
                        max_rows=_MAX_RESULT_ROWS,
                        max_bytes=_MAX_RESULT_BYTES,
                        max_in_flight_bytes=_MAX_RESULT_BYTES * 2,
                        memory_limit_bytes=64 * 1024 * 1024,
                        node_timeout_seconds=self._remaining_budget(record),
                    ),
                    max_concurrency=2,
                )
                async with record.lock:
                    record.coordinator = coordinator
                prepare_run = getattr(self.resolver, "prepare_run", None)
                if prepare_run is not None:
                    await prepare_run(request.run_id)
                await self._ensure_active(record)
                outcome = await coordinator.run(plan, run_id=request.run_id)
                if run_timeout.expired():
                    raise TimeoutError("The whole-run deadline has expired.")
                await self._ensure_active(record)
                await self._apply_runtime_events(record, outcome.events)
                if outcome.summary.state is ExecutionState.CANCELLED and record.cancel_requested:
                    raise asyncio.CancelledError
                if (
                    outcome.summary.state is not ExecutionState.SUCCEEDED
                    or outcome.manifest is None
                ):
                    raise RuntimeFailure(
                        outcome.summary.diagnostic_code or "RUNTIME_FAILED",
                        "The validated physical plan did not succeed.",
                    )
                await self._ensure_active(record)
                table = await store.read_page(
                    outcome.manifest.result,
                    0,
                    min(_MAX_RESULT_ROWS, outcome.manifest.row_count or _MAX_RESULT_ROWS),
                )
                await self._set_runtime_result(record, outcome, table)
                await self._complete_stage(record, "Execute", complete_nodes=False)

                await self._begin_stage(record, "Generate")
                await self._complete_stage(record, "Generate")
                if run_timeout.expired():
                    raise TimeoutError("The whole-run deadline has expired.")
                await self._succeed(record)
        except asyncio.CancelledError:
            await self._begin_terminal_cleanup(record)
            if record.authorization_error is not None:
                await self._authorization_failed(record)
            else:
                await self._cancelled(record)
        except (AccessDenied, PostgresError) as exc:
            await self._begin_terminal_cleanup(record)
            record.authorization_error = (
                "AUTHORIZATION_DENIED"
                if isinstance(exc, AccessDenied)
                else "AUTHORIZATION_UNAVAILABLE"
            )
            await self._authorization_failed(record)
        except TimeoutError:
            await self._begin_terminal_cleanup(record)
            cleanup = await self._resolver_cleanup_failure(record)
            await self._fail(
                record,
                cleanup.code if cleanup is not None else "RUN_TIMEOUT",
                (
                    "The resolver did not clean up safely after the run timeout."
                    if cleanup is not None
                    else "The bounded run exceeded its timeout."
                ),
            )
        except AdapterFailure as exc:
            await self._begin_terminal_cleanup(record)
            await self._fail(record, exc.code, str(exc))
        except RuntimeFailure as exc:
            await self._begin_terminal_cleanup(record)
            cleanup = await self._resolver_cleanup_failure(record)
            await self._fail(
                record,
                cleanup.code if cleanup is not None else exc.code,
                (
                    "The resolver did not clean up safely after runtime failure."
                    if cleanup is not None
                    else "The validated runtime operation failed safely."
                ),
            )
        except Exception:
            await self._begin_terminal_cleanup(record)
            cleanup = await self._resolver_cleanup_failure(record)
            await self._fail(
                record,
                cleanup.code if cleanup is not None else "INTEGRATION_FAILURE",
                (
                    "The resolver did not clean up safely after integration failure."
                    if cleanup is not None
                    else "The integration service could not complete the run."
                ),
            )
        finally:
            await self._stop_revocation_watch(record)
            async with record.lock:
                record.coordinator = None
            finish_run = getattr(self.resolver, "finish_run", None)
            if finish_run is not None:
                await finish_run(request.run_id)

    async def _begin_stage(
        self,
        record: RunRecord,
        name: str,
        *,
        plan: PhysicalPlan | None = None,
    ) -> None:
        await self._ensure_active(record)
        now = datetime.now(UTC)
        async with record.lock:
            stages = []
            for stage in record.status.stages:
                if stage.name == name:
                    nodes = (
                        [
                            NodeSummary(
                                node_id=node.id,
                                kind=node.operation.value,
                                state=(RunState.RUNNING if node.wave == 0 else RunState.QUEUED),
                                started_at=now if node.wave == 0 else None,
                            )
                            for node in plan.nodes
                        ]
                        if plan is not None
                        else [
                            node.model_copy(update={"state": RunState.RUNNING, "started_at": now})
                            for node in stage.nodes
                        ]
                    )
                    stage = stage.model_copy(
                        update={
                            "state": RunState.RUNNING,
                            "started_at": now,
                            "nodes": nodes,
                        }
                    )
                stages.append(stage)
            record.status = record.status.model_copy(
                update={"state": RunState.RUNNING, "stages": stages}
            )

    async def _complete_stage(
        self,
        record: RunRecord,
        name: str,
        *,
        complete_nodes: bool = True,
    ) -> None:
        now = datetime.now(UTC)
        async with record.lock:
            stages = []
            for stage in record.status.stages:
                if stage.name == name:
                    stage = stage.model_copy(
                        update={
                            "state": RunState.SUCCEEDED,
                            "completed_at": now,
                            "nodes": (
                                [
                                    node.model_copy(
                                        update={
                                            "state": RunState.SUCCEEDED,
                                            "completed_at": now,
                                        }
                                    )
                                    for node in stage.nodes
                                ]
                                if complete_nodes
                                else stage.nodes
                            ),
                        }
                    )
                stages.append(stage)
            record.status = record.status.model_copy(update={"stages": stages})

    async def _set_compile_artifact(
        self,
        record: RunRecord,
        response: CompileResponse,
    ) -> None:
        assert response.normalized_sqg is not None
        sqg = response.normalized_sqg
        filters = []
        for node in sqg.nodes:
            if node.operator is not Operator.FILTER:
                continue
            predicate = node.parameters.model_dump(mode="json")["predicate"]
            filters.append(
                SqgFilter(
                    field=predicate["column"],
                    operator=predicate["operator"],
                    value=json.dumps(
                        predicate["value"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
        summary = SqgSummary(
            version=sqg.schema_version,
            intent=record.request.question[:512],
            ontology=(
                f"{response.selected_semantic_context.ontology_id}@"
                f"{response.selected_semantic_context.ontology_version}"
            ),
            resolved_members=sorted(
                term.machine_id
                for term in response.resolved_terms
                if term.kind is ResolvedTermKind.MEMBER
            ),
            metrics=sorted(
                {
                    measure.source
                    for node in sqg.nodes
                    if isinstance(node.parameters, AggregateParameters)
                    for measure in node.parameters.measures
                }
            ),
            dimensions=sorted(
                {
                    concept
                    for node in sqg.nodes
                    for concept in self.adapter._node_concepts(node.parameters)
                    if concept.startswith("commerce.sales_record.")
                }
            ),
            filters=filters,
            policy_checks=["validated_sqg", "versioned_source_mapping"],
        )
        async with record.lock:
            record.status = record.status.model_copy(
                update={
                    "token_usage": TokenUsage(
                        input_tokens=response.token_metadata.input_tokens or 0,
                        output_tokens=response.token_metadata.output_tokens or 0,
                    )
                }
            )
            record.detail = record.detail.model_copy(update={"sqg": summary})

    async def _set_plan(self, record: RunRecord, plan: PhysicalPlan) -> None:
        nodes = [self._physical_node_detail(node) for node in plan.nodes]
        async with record.lock:
            record.physical_plan = plan
            record.detail = record.detail.model_copy(update={"physical_nodes": nodes})

    async def _set_runtime_result(
        self,
        record: RunRecord,
        outcome: RunOutcome,
        table: pa.Table,
    ) -> None:
        manifest = outcome.manifest
        assert manifest is not None
        columns = [
            ResultColumn(
                key=field.name,
                label=field.name.replace("_", " ").title(),
                data_type=self._scalar_type(field.data_type),
                format=self._column_format(field.name, field.data_type),
                nullable=field.nullable,
            )
            for field in manifest.schema_.fields
        ]
        row_objects = table.to_pylist()
        rows = [
            [self._json_scalar(row[column.key], column.data_type) for column in columns]
            for row in row_objects
        ]
        checksum_payload = json.dumps(
            {"columns": [column.key for column in columns], "rows": rows},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        result = ResultSet(
            columns=columns,
            rows=rows,
            row_count=manifest.row_count,
            truncated=manifest.row_count > len(rows),
        )
        committed = CommittedManifest(
            result_id=manifest.result.result_id,
            run_id=record.request.run_id,
            node_id=manifest.result.node_id,
            storage=ResultStorage(manifest.result.storage),
            uri=manifest.result.uri,
            row_count=manifest.row_count,
            byte_count=manifest.byte_count,
            checksum=f"sha256:{hashlib.sha256(checksum_payload).hexdigest()}",
            committed_at=manifest.committed_at,
        )
        lineage = LineageDetail(
            run_id=record.request.run_id,
            nodes=[
                LineageNodeDetail(
                    id=node.id,
                    kind=LineageNodeKind(node.kind),
                    operation=(
                        self._connector_resolver_name(record.request.run_id)
                        if node.kind == "source"
                        else node.operation
                    ),
                    source_alias=node.source_alias,
                    source_type=node.source_type,
                    result_id=node.result_id,
                    parameters=[
                        LineageParameter(
                            name=parameter["name"],
                            data_type=ScalarType(parameter["data_type"]),
                        )
                        for parameter in node.parameter_metadata
                    ],
                )
                for node in outcome.lineage.nodes
            ],
            edges=[
                LineageEdgeDetail(
                    source=(
                        edge.target
                        if edge.relation
                        in {
                            LineageRelation.READS_FROM.value,
                            LineageRelation.DEPENDS_ON.value,
                        }
                        else edge.source
                    ),
                    target=(
                        edge.source
                        if edge.relation
                        in {
                            LineageRelation.READS_FROM.value,
                            LineageRelation.DEPENDS_ON.value,
                        }
                        else edge.target
                    ),
                    relation=LineageRelation(edge.relation),
                )
                for edge in outcome.lineage.edges
            ],
        )
        locked = False
        try:
            async with self._publication_guard(record):
                await record.lock.acquire()
                locked = True
                if (
                    record.cancel_requested and not record.terminal_observed
                ) or record.authorization_error is not None:
                    raise asyncio.CancelledError
                detail = record.detail.model_copy(
                    update={"result": result, "manifest": committed, "lineage": lineage}
                )
                if serialized_detail_size(detail) > MAX_SERIALIZED_DETAIL_BYTES:
                    raise RuntimeFailure(
                        "DETAIL_SERIALIZATION_LIMIT",
                        "The typed run detail exceeded its serialized response boundary.",
                    )
                self._remaining_budget(record)
            record.pending_detail = detail
        finally:
            if locked:
                record.lock.release()

    def _connector_provenance(self, run_id: str) -> tuple[dict[str, str], ...]:
        reader = getattr(self.resolver, "provenance", None)
        if reader is None:
            return ()
        return tuple(
            {
                "run_id": item.run_id,
                "node_id": item.node_id,
                "resolver": item.resolver,
                "source_name": item.source_name,
                "statement_id": item.statement_id,
                "physical_fragment_sha256": item.physical_fragment_sha256,
            }
            for item in reader(run_id)
        )

    def _connector_resolver_name(self, run_id: str) -> str | None:
        provenance = self._connector_provenance(run_id)
        return provenance[0]["resolver"] if provenance else None

    async def _succeed(self, record: RunRecord) -> None:
        locked = False
        try:
            async with self._publication_guard(record):
                await record.lock.acquire()
                locked = True
                if record.status.state.terminal:
                    return
                if (
                    record.cancel_requested and not record.terminal_observed
                ) or record.authorization_error is not None:
                    raise asyncio.CancelledError
                self._remaining_budget(record)
                status = record.status.model_copy(
                    update={"state": RunState.SUCCEEDED, "finalized_at": datetime.now(UTC)}
                )
            # Publish only after the authorization transaction commits successfully.
            record.status = status
            if record.pending_detail is not None:
                record.detail = record.pending_detail
                record.pending_detail = None
        finally:
            if locked:
                record.lock.release()

    async def _cancelled(self, record: RunRecord) -> None:
        cleanup = await self._resolver_cleanup_failure(record)
        if cleanup is not None:
            await self._fail(
                record,
                cleanup.code,
                "The provider cancellation could not be confirmed safely.",
            )
            return
        await self._finalize_cancelled(record)

    async def _resolver_cleanup_failure(
        self,
        record: RunRecord,
    ) -> RuntimeFailure | None:
        wait_for_cleanup = getattr(self.resolver, "wait_for_run_cleanup", None)
        if wait_for_cleanup is None:
            return None
        try:
            await wait_for_cleanup(record.request.run_id)
        except RuntimeFailure as exc:
            return exc
        return None

    async def _finalize_cancelled(self, record: RunRecord) -> None:
        now = datetime.now(UTC)
        async with record.lock:
            if record.status.state.terminal:
                return
            stages = []
            for stage in record.status.stages:
                if stage.state in {RunState.STARTING, RunState.RUNNING}:
                    nodes = []
                    for node in stage.nodes:
                        if node.state.terminal or node.state is RunState.QUEUED:
                            nodes.append(node)
                        else:
                            nodes.append(
                                node.model_copy(
                                    update={
                                        "state": RunState.CANCELLED,
                                        "completed_at": now,
                                    }
                                )
                            )
                    stage = stage.model_copy(
                        update={
                            "state": RunState.CANCELLED,
                            "completed_at": now,
                            "nodes": nodes,
                        }
                    )
                stages.append(stage)
            record.status = record.status.model_copy(
                update={
                    "state": RunState.CANCELLED,
                    "finalized_at": now,
                    "stages": stages,
                }
            )

    async def _fail(self, record: RunRecord, code: str, message: str) -> None:
        now = datetime.now(UTC)
        async with record.lock:
            if record.status.state.terminal:
                return
            stages = []
            failed_stage: str | None = None
            for stage in record.status.stages:
                if stage.state in {RunState.STARTING, RunState.RUNNING}:
                    failed_stage = stage.name
                    nodes = []
                    for node in stage.nodes:
                        if node.state.terminal or node.state is RunState.QUEUED:
                            nodes.append(node)
                        else:
                            nodes.append(
                                node.model_copy(
                                    update={
                                        "state": RunState.FAILED,
                                        "completed_at": now,
                                    }
                                )
                            )
                    stage = stage.model_copy(
                        update={
                            "state": RunState.FAILED,
                            "completed_at": now,
                            "nodes": nodes,
                        }
                    )
                stages.append(stage)
            diagnostic = DiagnosticSummary(
                code=code[:64],
                message=message[:512],
                stage=failed_stage,
                occurred_at=now,
            )
            detail_diagnostic = DetailDiagnostic(
                sequence=len(record.detail.diagnostics),
                run_id=record.request.run_id,
                scope=DiagnosticScope.STAGE,
                scope_id=(failed_stage or "run").lower(),
                code=code[:128],
                title="Run failed safely",
                message=message[:1_000],
                recovery="Review the bounded diagnostic code and correct the semantic request.",
                severity=DiagnosticSeverity.ERROR,
                occurred_at=now,
            )
            record.status = record.status.model_copy(
                update={
                    "state": RunState.FAILED,
                    "finalized_at": now,
                    "stages": stages,
                    "diagnostics": [*record.status.diagnostics, diagnostic][-100:],
                }
            )
            record.detail = record.detail.model_copy(
                update={"diagnostics": [*record.detail.diagnostics, detail_diagnostic][-1_000:]}
            )

    async def _ensure_active(self, record: RunRecord) -> None:
        async with record.lock:
            if (
                record.cancel_requested and not record.terminal_observed
            ) or record.status.state is RunState.CANCELLED:
                raise asyncio.CancelledError
            self._remaining_budget(record)
        await self._authorize_request(record.trusted_context, "run.contributor")

    async def _authorize_request(self, context: TrustedContext | None, permission: str) -> None:
        if self._legacy:
            if context is not None:
                raise AccessDenied("Legacy mode does not accept enterprise context.")
            return
        if context is None or self.authority is None:
            raise AccessDenied("An explicitly verified context is required.")
        await self.authority.reauthorize(context, permission)

    @asynccontextmanager
    async def _publication_guard(self, record: RunRecord) -> AsyncIterator[None]:
        if self._legacy:
            yield
            return
        if self.authority is None or record.trusted_context is None:
            raise AccessDenied("An explicitly verified context is required.")
        async with self.authority.guard(
            record.trusted_context, permission="run.contributor", deadline=record.deadline
        ):
            yield

    async def _watch_authorization(self, record: RunRecord) -> None:
        while True:
            await asyncio.sleep(0.25)
            async with record.lock:
                if record.status.state.terminal or record.terminal_cleanup_started:
                    return
            try:
                await self._authorize_request(record.trusted_context, "run.contributor")
            except (AccessDenied, PostgresError, TimeoutError) as exc:
                async with record.lock:
                    if record.status.state.terminal or record.terminal_cleanup_started:
                        return
                    record.authorization_error = (
                        "AUTHORIZATION_DENIED"
                        if isinstance(exc, AccessDenied)
                        else "AUTHORIZATION_UNAVAILABLE"
                    )
                    task = record.task
                    if task is not None and not task.done() and not task.cancelling():
                        task.cancel()
                return

    async def _begin_terminal_cleanup(self, record: RunRecord) -> None:
        # Set synchronously before any await: terminal drain has a single cancellation owner.
        record.terminal_cleanup_started = True
        await self._stop_revocation_watch(record)

    @staticmethod
    async def _stop_revocation_watch(record: RunRecord) -> None:
        if record.revocation_task is not None:
            record.revocation_task.cancel()
            try:
                await record.revocation_task
            except asyncio.CancelledError:
                pass

    async def _authorization_failed(self, record: RunRecord) -> None:
        cleanup = await self._resolver_cleanup_failure(record)
        async with record.lock:
            if not record.status.state.terminal:
                record.detail = record.detail.model_copy(update={"result": None, "manifest": None})
                record.pending_detail = None
        await self._fail(
            record,
            cleanup.code
            if cleanup is not None
            else record.authorization_error or "AUTHORIZATION_DENIED",
            "Workspace authorization ended; active work was cancelled and no result was released.",
        )

    @staticmethod
    def _remaining_budget(record: RunRecord) -> float:
        remaining = (
            _RUN_TIMEOUT_SECONDS
            if record.deadline is None
            else record.deadline - asyncio.get_running_loop().time()
        )
        if remaining <= 0:
            raise TimeoutError("The whole-run deadline has expired.")
        return remaining

    async def _apply_runtime_events(
        self,
        record: RunRecord,
        events: tuple[DiagnosticEvent, ...] | list[DiagnosticEvent],
    ) -> None:
        runtime_states = {
            ExecutionState.PENDING: RunState.QUEUED,
            ExecutionState.READY: RunState.STARTING,
            ExecutionState.RUNNING: RunState.RUNNING,
            ExecutionState.SUCCEEDED: RunState.SUCCEEDED,
            ExecutionState.CANCELLED: RunState.CANCELLED,
            ExecutionState.FAILED: RunState.FAILED,
            ExecutionState.TIMED_OUT: RunState.FAILED,
            ExecutionState.SKIPPED: RunState.CANCELLED,
        }
        by_node: dict[str, list[Any]] = {}
        for event in events:
            if event.scope == "node":
                by_node.setdefault(event.scope_id, []).append(event)
        async with record.lock:
            stages = []
            for stage in record.status.stages:
                if stage.name != "Execute":
                    stages.append(stage)
                    continue
                nodes = []
                for node in stage.nodes:
                    events = by_node.get(node.node_id, [])
                    if not events:
                        nodes.append(node)
                        continue
                    running = next(
                        (event for event in events if event.state is ExecutionState.RUNNING),
                        None,
                    )
                    terminal = next(
                        (
                            event
                            for event in reversed(events)
                            if event.state
                            in {
                                ExecutionState.SUCCEEDED,
                                ExecutionState.CANCELLED,
                                ExecutionState.FAILED,
                                ExecutionState.TIMED_OUT,
                                ExecutionState.SKIPPED,
                            }
                        ),
                        None,
                    )
                    latest = terminal or events[-1]
                    state = runtime_states[latest.state]
                    started_at = (
                        running.timestamp
                        if running is not None
                        else (latest.timestamp if state is not RunState.QUEUED else None)
                    )
                    nodes.append(
                        node.model_copy(
                            update={
                                "state": state,
                                "started_at": started_at,
                                "completed_at": latest.timestamp if state.terminal else None,
                            }
                        )
                    )
                stages.append(stage.model_copy(update={"nodes": nodes}))
            record.status = record.status.model_copy(update={"stages": stages})

    @staticmethod
    def _physical_node_detail(node: PhysicalNode) -> PhysicalNodeDetail:
        output_fields: list[str] = []
        operation = node.operator
        if node.source_fragment is not None:
            output_fields.extend(
                column.column_name for column in node.source_fragment.bound_columns
            )
            operation = node.source_fragment.operations[-1]
        if operation is not None:
            output_fields.extend(operation.columns)
            output_fields.extend(operation.group_by)
            output_fields.extend(item.name for item in operation.aggregates)
            output_fields.extend(item.name for item in operation.expressions)
            output_fields.extend(operation.pivot_index)
            output_fields.extend(operation.pivot_values)
        output_fields = list(dict.fromkeys(output_fields))[:100]
        return PhysicalNodeDetail(
            id=node.id,
            kind=OperatorKind(node.operation.value),
            label=f"{node.operation.value.title()} node",
            plain_language=(
                f"Executes the validated {node.operation.value} operation "
                "without accepting provider SQL."
            ),
            inputs=list(node.dependencies),
            output_fields=output_fields,
        )

    @staticmethod
    def _scalar_type(data_type: str) -> ScalarType:
        normalized = data_type.casefold()
        if normalized.startswith("timestamp"):
            return ScalarType.TIMESTAMP
        if normalized.startswith("date"):
            return ScalarType.DATE
        if normalized.startswith(("int", "uint")):
            return ScalarType.INTEGER
        if normalized.startswith("decimal"):
            return ScalarType.DECIMAL
        if normalized in {"double", "float", "halffloat"}:
            return ScalarType.FLOAT
        if normalized in {"bool", "boolean"}:
            return ScalarType.BOOLEAN
        return ScalarType.STRING

    @staticmethod
    def _column_format(name: str, data_type: str) -> ColumnFormat:
        scalar = OrchestrationService._scalar_type(data_type)
        if scalar is ScalarType.TIMESTAMP:
            return ColumnFormat.TIMESTAMP
        if scalar is ScalarType.DATE:
            return ColumnFormat.DATE
        if "profit" in name.casefold():
            return ColumnFormat.CURRENCY
        if scalar in {ScalarType.INTEGER, ScalarType.FLOAT, ScalarType.DECIMAL}:
            return ColumnFormat.NUMBER
        return ColumnFormat.TEXT

    @staticmethod
    def _json_scalar(
        value: Any,
        data_type: ScalarType,
    ) -> str | int | float | bool | None:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Decimal):
            if data_type is ScalarType.DECIMAL:
                return format(value, "f")
            raise RuntimeFailure(
                "RESULT_DECIMAL_TYPE_MISMATCH",
                "An exact decimal result did not declare the decimal scalar type.",
            )
        if isinstance(value, datetime):
            aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
            return aware.isoformat().replace("+00:00", "Z")
        if isinstance(value, date):
            return value.isoformat()
        if data_type is ScalarType.STRING:
            return str(value)
        raise RuntimeFailure(
            "RESULT_SCALAR_UNSUPPORTED",
            "The result contained a value outside the typed scalar contract.",
        )
