from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from query_runtime.coordinator import QueryCoordinator
from query_runtime.domain import ExecutionState, PhysicalPlan
from query_runtime.errors import RuntimeFailure
from query_runtime.operators import ResourceLimits
from query_runtime.planner import CapabilityPlanner
from query_runtime.resolver import FakeResolver
from query_runtime.result_store import InlineResultStore
from semantic_api.compiler import SemanticCompiler
from semantic_api.models import CompileRequest, CompileStatus, InitializeRequest

from semantic_backend.adapter import AdapterFailure, CompilerRuntimeAdapter
from semantic_backend.fake_source import load_synthetic_sales
from semantic_backend.models import (
    CompileArtifact,
    DiagnosticSummary,
    LineageDetail,
    LineageEdgeDetail,
    LineageNodeDetail,
    NodeSummary,
    PhysicalNodeSummary,
    PhysicalPlanSummary,
    ResultColumn,
    ResultDetail,
    ResultManifestSummary,
    RunDetail,
    RunState,
    RunStatus,
    StageSummary,
    StartRunRequest,
    TokenUsage,
)
from semantic_backend.repository import InMemoryRunRepository, RunRecord, RunRepository

_STAGES = ("Initialize", "Compile", "Optimize", "Execute", "Generate")


class OrchestrationService:
    def __init__(
        self,
        *,
        compiler: SemanticCompiler | None = None,
        adapter: CompilerRuntimeAdapter | None = None,
        repository: RunRepository | None = None,
        resolver: FakeResolver | None = None,
    ) -> None:
        self.compiler = compiler or SemanticCompiler.default()
        self.adapter = adapter or CompilerRuntimeAdapter()
        self.repository = repository or InMemoryRunRepository()
        if resolver is None:
            mapping = self.adapter.mapping
            source = mapping.source
            catalog = self._catalog()
            resolver = FakeResolver(
                tables={source.alias: load_synthetic_sales()},
                catalogs={source.alias: catalog},
            )
        self.resolver = resolver
        self._semaphore = asyncio.Semaphore(4)

    def _catalog(self) -> Any:
        from query_runtime.domain import AggregateFunction, CapabilityCatalog, OperatorKind

        source = self.adapter.mapping.source
        return CapabilityCatalog(
            source_alias=source.alias,
            source_type=source.source_type,
            operator_kinds=frozenset(
                {
                    OperatorKind.SELECT,
                    OperatorKind.FILTER,
                    OperatorKind.AGGREGATE,
                    OperatorKind.SORT,
                }
            ),
            aggregate_functions=frozenset(AggregateFunction),
            max_rows=100_000,
            max_bytes=32 * 1024 * 1024,
        )

    async def ready(self) -> bool:
        return self.compiler.ready and await self.resolver.health()

    async def start(self, request: StartRunRequest) -> RunStatus:
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
        record, created = await self.repository.create(request, status)
        if created:
            record.task = asyncio.create_task(
                self._run(record),
                name=f"semantic-backend-{request.run_id}",
            )
        return record.status.model_copy(deep=True)

    async def get_status(self, run_id: str) -> RunStatus:
        record = await self.repository.get(run_id)
        async with record.lock:
            return record.status.model_copy(deep=True)

    async def get_detail(self, run_id: str) -> RunDetail:
        record = await self.repository.get(run_id)
        async with record.lock:
            return record.detail.model_copy(update={"status": record.status}, deep=True)

    async def cancel(self, run_id: str) -> RunStatus:
        record = await self.repository.get(run_id)
        async with record.lock:
            if record.status.state.terminal:
                return record.status.model_copy(deep=True)
            record.cancel_requested = True
            now = datetime.now(UTC)
            stages = []
            for stage in record.status.stages:
                if stage.state in {RunState.STARTING, RunState.RUNNING}:
                    nodes = [
                        node.model_copy(
                            update={
                                "state": RunState.CANCELLED,
                                "completed_at": now,
                            }
                        )
                        for node in stage.nodes
                    ]
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
            task = record.task
            coordinator = record.coordinator
            self._sync_detail(record)
        if coordinator is not None:
            await coordinator.cancel(run_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return record.status.model_copy(deep=True)

    async def shutdown(self) -> None:
        records = await self.repository.list_records()
        tasks = [
            record.task for record in records if record.task is not None and not record.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, record: RunRecord) -> None:
        request = record.request
        try:
            async with self._semaphore:
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
                )
                if compile_response.status is not CompileStatus.SUCCEEDED:
                    code = (
                        compile_response.diagnostics[0].code
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
                )
                planner = CapabilityPlanner(
                    binder=adapted.binder,
                    sources=adapted.sources,
                    capabilities=adapted.capabilities,
                )
                plan = planner.plan(adapted.graph)
                await self._set_plan(record, plan)
                await self._complete_stage(record, "Optimize")

                await self._begin_stage(record, "Execute", plan=plan)
                store = InlineResultStore()
                options = request.execution_options
                coordinator = QueryCoordinator(
                    resolver=self.resolver,
                    result_store=store,
                    limits=ResourceLimits(
                        max_rows=options.max_rows,
                        max_bytes=options.max_bytes,
                        max_in_flight_bytes=options.max_bytes * 2,
                        memory_limit_bytes=options.max_bytes,
                        node_timeout_seconds=options.timeout_seconds,
                    ),
                    max_concurrency=2,
                )
                async with record.lock:
                    record.coordinator = coordinator
                async with asyncio.timeout(options.timeout_seconds):
                    outcome = await coordinator.run(plan, run_id=request.run_id)
                if (
                    outcome.summary.state is not ExecutionState.SUCCEEDED
                    or outcome.manifest is None
                ):
                    raise RuntimeFailure(
                        outcome.summary.diagnostic_code or "RUNTIME_FAILED",
                        "The validated physical plan did not succeed.",
                    )
                table = await store.read_page(
                    outcome.manifest.result,
                    0,
                    min(options.max_rows, outcome.manifest.row_count or options.max_rows),
                )
                await self._set_runtime_result(record, outcome, table.to_pylist())
                await self._complete_stage(record, "Execute")

                await self._begin_stage(record, "Generate")
                await self._complete_stage(record, "Generate")
                await self._succeed(record)
        except asyncio.CancelledError:
            await self._cancelled(record)
        except TimeoutError:
            await self._fail(record, "RUN_TIMEOUT", "The bounded run exceeded its timeout.")
        except AdapterFailure as exc:
            await self._fail(record, exc.code, str(exc))
        except RuntimeFailure as exc:
            await self._fail(
                record,
                exc.code,
                "The validated runtime operation failed safely.",
            )
        except Exception:
            await self._fail(
                record,
                "INTEGRATION_FAILURE",
                "The integration service could not complete the run.",
            )
        finally:
            async with record.lock:
                record.coordinator = None

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
                    if plan is None:
                        nodes = [
                            node.model_copy(
                                update={
                                    "state": RunState.RUNNING,
                                    "started_at": now,
                                }
                            )
                            for node in stage.nodes
                        ]
                    else:
                        nodes = [
                            NodeSummary(
                                node_id=node.id[:64],
                                kind=node.operation.value,
                                state=RunState.RUNNING,
                                started_at=now,
                            )
                            for node in plan.nodes
                        ]
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
            self._sync_detail(record)

    async def _complete_stage(self, record: RunRecord, name: str) -> None:
        now = datetime.now(UTC)
        async with record.lock:
            stages = []
            for stage in record.status.stages:
                if stage.name == name:
                    nodes = [
                        node.model_copy(
                            update={
                                "state": RunState.SUCCEEDED,
                                "completed_at": now,
                            }
                        )
                        for node in stage.nodes
                    ]
                    stage = stage.model_copy(
                        update={
                            "state": RunState.SUCCEEDED,
                            "completed_at": now,
                            "nodes": nodes,
                        }
                    )
                stages.append(stage)
            record.status = record.status.model_copy(update={"stages": stages})
            self._sync_detail(record)

    async def _set_compile_artifact(self, record: RunRecord, response: Any) -> None:
        assert response.normalized_sqg is not None
        artifact = CompileArtifact(
            normalized_sqg=response.normalized_sqg,
            resolved_terms=[item.model_dump(mode="json") for item in response.resolved_terms],
            timing_ms={
                key: float(value) for key, value in response.timing_metadata.model_dump().items()
            },
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
            record.detail = record.detail.model_copy(update={"compile_artifact": artifact})
            self._sync_detail(record)

    async def _set_plan(self, record: RunRecord, plan: PhysicalPlan) -> None:
        summary = PhysicalPlanSummary(
            plan_id=plan.id,
            output_node_id=plan.output_node_id,
            nodes=[
                PhysicalNodeSummary(
                    node_id=node.id,
                    operation=node.operation.value,
                    kind=node.kind.value,
                    dependencies=list(node.dependencies),
                    logical_node_ids=list(node.logical_node_ids),
                    source_alias=(
                        node.source_fragment.source.alias
                        if node.source_fragment is not None
                        else None
                    ),
                )
                for node in plan.nodes
            ],
        )
        async with record.lock:
            record.detail = record.detail.model_copy(update={"physical_plan": summary})
            self._sync_detail(record)

    async def _set_runtime_result(
        self,
        record: RunRecord,
        outcome: Any,
        rows: list[dict[str, Any]],
    ) -> None:
        manifest = outcome.manifest
        assert manifest is not None
        result = ResultDetail(
            columns=[
                ResultColumn(
                    name=field.name,
                    data_type=field.data_type,
                    nullable=field.nullable,
                )
                for field in manifest.schema_.fields
            ],
            rows=[{key: self._json_scalar(value) for key, value in row.items()} for row in rows],
            manifest=ResultManifestSummary(
                result_id=manifest.result.result_id,
                storage=manifest.result.storage,
                row_count=manifest.row_count,
                byte_count=manifest.byte_count,
                committed_at=manifest.committed_at,
            ),
        )
        lineage = LineageDetail(
            nodes=[
                LineageNodeDetail(
                    id=node.id,
                    kind=node.kind,
                    operation=node.operation,
                    source_alias=node.source_alias,
                    source_type=node.source_type,
                    result_id=node.result_id,
                    parameter_metadata=list(node.parameter_metadata),
                )
                for node in outcome.lineage.nodes
            ],
            edges=[
                LineageEdgeDetail(
                    source=edge.source,
                    target=edge.target,
                    relation=edge.relation,
                )
                for edge in outcome.lineage.edges
            ],
        )
        async with record.lock:
            record.detail = record.detail.model_copy(update={"result": result, "lineage": lineage})
            self._sync_detail(record)

    async def _succeed(self, record: RunRecord) -> None:
        async with record.lock:
            if record.status.state is RunState.CANCELLED:
                return
            record.status = record.status.model_copy(
                update={
                    "state": RunState.SUCCEEDED,
                    "finalized_at": datetime.now(UTC),
                }
            )
            self._sync_detail(record)

    async def _cancelled(self, record: RunRecord) -> None:
        async with record.lock:
            if record.status.state is RunState.CANCELLED:
                return
        await self.cancel(record.request.run_id)

    async def _fail(self, record: RunRecord, code: str, message: str) -> None:
        now = datetime.now(UTC)
        async with record.lock:
            if record.status.state is RunState.CANCELLED:
                return
            stages = []
            failed_stage: str | None = None
            for stage in record.status.stages:
                if stage.state in {RunState.STARTING, RunState.RUNNING}:
                    failed_stage = stage.name
                    nodes = [
                        node.model_copy(
                            update={
                                "state": RunState.FAILED,
                                "completed_at": now,
                            }
                        )
                        for node in stage.nodes
                    ]
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
            record.status = record.status.model_copy(
                update={
                    "state": RunState.FAILED,
                    "finalized_at": now,
                    "stages": stages,
                    "diagnostics": [*record.status.diagnostics, diagnostic][-100:],
                }
            )
            self._sync_detail(record)

    async def _ensure_active(self, record: RunRecord) -> None:
        async with record.lock:
            if record.cancel_requested or record.status.state is RunState.CANCELLED:
                raise asyncio.CancelledError

    @staticmethod
    def _json_scalar(value: Any) -> str | int | float | bool | None:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _sync_detail(record: RunRecord) -> None:
        record.detail = record.detail.model_copy(update={"status": record.status})
