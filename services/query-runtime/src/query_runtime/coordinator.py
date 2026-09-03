"""Bounded, observable DAG coordinator for validated physical plans."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass

import pyarrow as pa

from query_runtime.domain import (
    TERMINAL_STATES,
    CommittedManifest,
    DiagnosticEvent,
    ExecutionState,
    LineageGraph,
    PhysicalNode,
    PhysicalNodeKind,
    PhysicalPlan,
    ResultSummary,
)
from query_runtime.errors import (
    DeferredCleanupCancellation,
    PlanFailure,
    ResolverFailure,
    ResourceLimitFailure,
    RuntimeFailure,
)
from query_runtime.events import EventStore, InMemoryEventStore, StateMachine
from query_runtime.lineage import LineageRecorder
from query_runtime.operators import DuckDBOperatorExecutor, ResourceLimits
from query_runtime.resolver import SourceResolver
from query_runtime.result_store import ResultStore


@dataclass(frozen=True)
class RunOutcome:
    summary: ResultSummary
    manifest: CommittedManifest | None
    lineage: LineageGraph
    events: tuple[DiagnosticEvent, ...]


@dataclass(frozen=True)
class _NodeOutcome:
    node_id: str
    state: ExecutionState
    table: pa.Table | None = None
    manifest: CommittedManifest | None = None
    diagnostic_code: str | None = None


class _MemoryBudget:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.current = 0
        self._reservations: dict[tuple[str, str], int] = {}
        self._deferred: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()

    async def reserve(self, run_id: str, node_id: str, size: int) -> None:
        async with self._lock:
            if self.current + size > self.maximum:
                raise ResourceLimitFailure(
                    "LIMIT_IN_FLIGHT_BYTES_EXCEEDED",
                    "Run exceeded its global in-flight table memory budget",
                    details={
                        "current_bytes": self.current,
                        "requested_bytes": size,
                        "limit": self.maximum,
                    },
                )
            self.current += size
            self._reservations[(run_id, node_id)] = size

    async def release(self, run_id: str, node_id: str) -> None:
        async with self._lock:
            key = (run_id, node_id)
            size = self._reservations.pop(key, 0)
            self._deferred.discard(key)
            self.current = max(0, self.current - size)

    async def defer(self, run_id: str, node_id: str) -> None:
        async with self._lock:
            key = (run_id, node_id)
            if key in self._reservations:
                self._deferred.add(key)

    async def release_run(self, run_id: str) -> None:
        async with self._lock:
            keys = [
                key
                for key in self._reservations
                if key[0] == run_id and key not in self._deferred
            ]
            released = sum(self._reservations.pop(key) for key in keys)
            self.current = max(0, self.current - released)


class _Emitter:
    def __init__(self, run_id: str, store: EventStore) -> None:
        self.run_id = run_id
        self.store = store
        self._sequence = 0
        self._lock = asyncio.Lock()

    async def emit(
        self,
        *,
        scope: str,
        scope_id: str,
        state: ExecutionState,
        code: str,
        message: str,
        duration_ms: int | None = None,
        metadata: dict[str, str | int | bool | None] | None = None,
    ) -> None:
        async with self._lock:
            sequence = self._sequence
            self._sequence += 1
        await self.store.append(
            DiagnosticEvent(
                sequence=sequence,
                run_id=self.run_id,
                scope=scope,  # type: ignore[arg-type]
                scope_id=scope_id,
                state=state,
                code=code,
                message=message,
                duration_ms=duration_ms,
                metadata=metadata or {},
            )
        )


class QueryCoordinator:
    def __init__(
        self,
        *,
        resolver: SourceResolver,
        result_store: ResultStore,
        event_store: EventStore | None = None,
        limits: ResourceLimits | None = None,
        max_concurrency: int = 4,
    ) -> None:
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency <= 0
        ):
            raise ValueError("max_concurrency must be a positive integer")
        self.resolver = resolver
        self.result_store = result_store
        self.event_store = event_store or InMemoryEventStore()
        self.limits = limits or ResourceLimits()
        self.executor = DuckDBOperatorExecutor(self.limits)
        self.max_concurrency = max_concurrency
        self._cancellations: dict[str, asyncio.Event] = {}
        self._accepting_cancellation: set[str] = set()
        self._known_run_ids: set[str] = set()
        self._claiming_run_ids: set[str] = set()
        self._run_lock = asyncio.Lock()
        self._memory = _MemoryBudget(self.limits.max_in_flight_bytes)
        self._deferred_releases: set[asyncio.Task[None]] = set()

    async def cancel(self, run_id: str) -> bool:
        async with self._run_lock:
            event = self._cancellations.get(run_id)
            if event is None or run_id not in self._accepting_cancellation:
                return False
            event.set()
            return True

    async def run(self, plan: PhysicalPlan, *, run_id: str | None = None) -> RunOutcome:
        validate_physical_plan(plan)
        run_id = run_id or f"run-{uuid.uuid4().hex}"
        cancel_event = asyncio.Event()
        emitter = _Emitter(run_id, self.event_store)
        run_machine = StateMachine()
        recorder = LineageRecorder(run_id, plan)
        tables: dict[str, pa.Table] = {}
        manifests: dict[str, CommittedManifest] = {}
        remaining_consumers = {node.id: 0 for node in plan.nodes}
        for node in plan.nodes:
            for dependency in node.dependencies:
                remaining_consumers[dependency] += 1
        final_state = ExecutionState.FAILED
        diagnostic_code: str | None = None
        run_started = time.monotonic()
        nodes_by_wave: dict[int, list[PhysicalNode]] = defaultdict(list)
        for node in plan.nodes:
            nodes_by_wave[node.wave].append(node)

        try:
            await self._claim_run(run_id, cancel_event)
            run_machine.transition(ExecutionState.READY)
            await emitter.emit(
                scope="run",
                scope_id=run_id,
                state=run_machine.state,
                code="RUN_READY",
                message="Validated physical plan is ready",
            )
            run_machine.transition(ExecutionState.RUNNING)
            await emitter.emit(
                scope="run",
                scope_id=run_id,
                state=run_machine.state,
                code="RUN_STARTED",
                message="Run started",
            )
            semaphore = asyncio.Semaphore(self.max_concurrency)
            for wave in sorted(nodes_by_wave):
                wave_nodes = sorted(nodes_by_wave[wave], key=lambda item: item.id)
                if cancel_event.is_set():
                    await self._terminalize_waves(
                        nodes_by_wave,
                        first_wave=wave,
                        emitter=emitter,
                        state=ExecutionState.CANCELLED,
                    )
                    final_state = ExecutionState.CANCELLED
                    diagnostic_code = "RUN_CANCELLED"
                    break
                stage_id = f"wave-{wave}"
                stage_machine = StateMachine()
                stage_started = time.monotonic()
                stage_machine.transition(ExecutionState.READY)
                await emitter.emit(
                    scope="stage",
                    scope_id=stage_id,
                    state=stage_machine.state,
                    code="STAGE_READY",
                    message="Stage dependencies are satisfied",
                    metadata={"wave": wave},
                )
                stage_machine.transition(ExecutionState.RUNNING)
                await emitter.emit(
                    scope="stage",
                    scope_id=stage_id,
                    state=stage_machine.state,
                    code="STAGE_STARTED",
                    message="Stage started",
                    metadata={"wave": wave},
                )
                outcomes = await asyncio.gather(
                    *(
                        self._execute_node(
                            node=node,
                            run_id=run_id,
                            tables=tables,
                            cancel_event=cancel_event,
                            semaphore=semaphore,
                            emitter=emitter,
                            memory=self._memory,
                        )
                        for node in wave_nodes
                    )
                )
                stage_state = _terminal_stage_state(outcomes)
                stage_diagnostic = next(
                    (
                        item.diagnostic_code
                        for item in outcomes
                        if item.diagnostic_code is not None
                    ),
                    f"RUN_{stage_state.value}",
                )
                release_outputs: list[str] = []
                for outcome in outcomes:
                    if outcome.table is not None and outcome.manifest is not None:
                        manifests[outcome.node_id] = outcome.manifest
                        recorder.record_result(outcome.node_id, outcome.manifest)
                        if remaining_consumers[outcome.node_id] > 0:
                            tables[outcome.node_id] = outcome.table
                        else:
                            release_outputs.append(outcome.node_id)
                del outcome
                del outcomes
                for node_id in release_outputs:
                    await self._memory.release(run_id, node_id)
                await self._release_consumed_tables(
                    wave_nodes,
                    remaining_consumers,
                    tables,
                    self._memory,
                    run_id,
                )
                stage_machine.transition(stage_state)
                await emitter.emit(
                    scope="stage",
                    scope_id=stage_id,
                    state=stage_state,
                    code=f"STAGE_{stage_state.value}",
                    message=f"Stage {stage_state.value.lower()}",
                    duration_ms=_duration(stage_started),
                    metadata={"wave": wave},
                )
                if stage_state is not ExecutionState.SUCCEEDED:
                    cancel_event.set()
                    final_state = stage_state
                    diagnostic_code = stage_diagnostic
                    await self._terminalize_waves(
                        nodes_by_wave,
                        first_wave=wave + 1,
                        emitter=emitter,
                        state=(
                            ExecutionState.CANCELLED
                            if stage_state is ExecutionState.CANCELLED
                            else ExecutionState.SKIPPED
                        ),
                    )
                    break
            else:
                final_state = ExecutionState.SUCCEEDED

            async with self._run_lock:
                self._accepting_cancellation.discard(run_id)
                cancelled_before_terminal = cancel_event.is_set()
            if (
                final_state is ExecutionState.SUCCEEDED
                and cancelled_before_terminal
            ):
                final_state = ExecutionState.CANCELLED
                diagnostic_code = "RUN_CANCELLED"
            run_machine.transition(final_state)
            await emitter.emit(
                scope="run",
                scope_id=run_id,
                state=final_state,
                code=f"RUN_{final_state.value}",
                message=f"Run {final_state.value.lower()}",
                duration_ms=_duration(run_started),
            )
        except asyncio.CancelledError:
            cancel_event.set()
            if await self._owns_run(run_id, cancel_event):
                async with self._run_lock:
                    self._accepting_cancellation.discard(run_id)
                finalizer = asyncio.create_task(
                    self._finalize_outer_cancellation(plan, emitter)
                )
                while not finalizer.done():
                    try:
                        await asyncio.shield(finalizer)
                    except asyncio.CancelledError:
                        continue
                finalizer.result()
            raise
        finally:
            if await self._owns_run(run_id, cancel_event):
                await self._memory.release_run(run_id)
                tables.clear()
                async with self._run_lock:
                    self._accepting_cancellation.discard(run_id)
                    if self._cancellations.get(run_id) is cancel_event:
                        self._cancellations.pop(run_id, None)

        manifest = (
            manifests.get(plan.output_node_id)
            if final_state is ExecutionState.SUCCEEDED
            else None
        )
        if final_state is ExecutionState.SUCCEEDED and manifest is None:
            raise RuntimeFailure(
                "RUN_OUTPUT_MISSING", "Successful run did not produce an output manifest"
            )
        summary = ResultSummary(
            run_id=run_id,
            state=final_state,
            row_count=manifest.row_count if manifest else None,
            result=manifest.result if manifest else None,
            diagnostic_code=diagnostic_code,
        )
        return RunOutcome(
            summary=summary,
            manifest=manifest,
            lineage=recorder.graph(),
            events=await self.event_store.list(run_id),
        )

    async def _execute_node(
        self,
        *,
        node: PhysicalNode,
        run_id: str,
        tables: dict[str, pa.Table],
        cancel_event: asyncio.Event,
        semaphore: asyncio.Semaphore,
        emitter: _Emitter,
        memory: _MemoryBudget,
    ) -> _NodeOutcome:
        machine = StateMachine()
        started = time.monotonic()
        reserved = False
        machine.transition(ExecutionState.READY)
        await emitter.emit(
            scope="node",
            scope_id=node.id,
            state=machine.state,
            code="NODE_READY",
            message="Node dependencies are satisfied",
            metadata={"wave": node.wave, "operation": node.operation.value},
        )
        try:
            async with semaphore:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                machine.transition(ExecutionState.RUNNING)
                await emitter.emit(
                    scope="node",
                    scope_id=node.id,
                    state=machine.state,
                    code="NODE_STARTED",
                    message="Node started",
                    metadata={"wave": node.wave, "operation": node.operation.value},
                )
                inputs = tuple(tables[dependency] for dependency in node.dependencies)
                table, manifest = await asyncio.wait_for(
                    self._perform_and_commit(
                        node, run_id, inputs, cancel_event, memory
                    ),
                    timeout=self.limits.node_timeout_seconds,
                )
                reserved = True
                if cancel_event.is_set():
                    await memory.release(run_id, node.id)
                    reserved = False
                    raise asyncio.CancelledError
                machine.transition(ExecutionState.SUCCEEDED)
                await emitter.emit(
                    scope="node",
                    scope_id=node.id,
                    state=machine.state,
                    code="NODE_SUCCEEDED",
                    message="Node succeeded",
                    duration_ms=_duration(started),
                    metadata={
                        "wave": node.wave,
                        "operation": node.operation.value,
                        "rows": table.num_rows,
                    },
                )
                return _NodeOutcome(
                    node.id,
                    machine.state,
                    table,
                    manifest,
                )
        except TimeoutError:
            if reserved:
                await memory.release(run_id, node.id)
            cancel_event.set()
            if node.kind is PhysicalNodeKind.SOURCE_FRAGMENT:
                await self._cancel_resolver(run_id, node.id, emitter)
            machine.transition(ExecutionState.TIMED_OUT)
            await emitter.emit(
                scope="node",
                scope_id=node.id,
                state=machine.state,
                code="NODE_TIMEOUT",
                message="Node exceeded its configured execution time",
                duration_ms=_duration(started),
            )
            return _NodeOutcome(
                node.id, machine.state, diagnostic_code="NODE_TIMEOUT"
            )
        except asyncio.CancelledError:
            if reserved:
                await memory.release(run_id, node.id)
            cancel_event.set()
            if node.kind is PhysicalNodeKind.SOURCE_FRAGMENT:
                await self._cancel_resolver(run_id, node.id, emitter)
            machine.transition(ExecutionState.CANCELLED)
            await emitter.emit(
                scope="node",
                scope_id=node.id,
                state=machine.state,
                code="NODE_CANCELLED",
                message="Node was cancelled",
                duration_ms=_duration(started),
            )
            return _NodeOutcome(
                node.id, machine.state, diagnostic_code="NODE_CANCELLED"
            )
        except Exception as exc:
            if reserved:
                await memory.release(run_id, node.id)
            cancel_event.set()
            code = exc.code if isinstance(exc, RuntimeFailure) else "NODE_FAILED"
            if isinstance(exc, ResolverFailure):
                message = "Source resolver failed"
            elif isinstance(exc, RuntimeFailure):
                message = exc.message
            else:
                message = "Node failed without publishing an output"
            machine.transition(ExecutionState.FAILED)
            await emitter.emit(
                scope="node",
                scope_id=node.id,
                state=machine.state,
                code=code,
                message=message,
                duration_ms=_duration(started),
            )
            return _NodeOutcome(node.id, machine.state, diagnostic_code=code)

    async def _perform_node(
        self,
        node: PhysicalNode,
        inputs: tuple[pa.Table, ...],
        cancel_event: asyncio.Event,
    ) -> pa.Table:
        if node.kind is PhysicalNodeKind.SOURCE_FRAGMENT:
            assert node.source_fragment is not None
            return await self.resolver.execute(node.source_fragment, cancel_event)
        assert node.operator is not None
        return await self.executor.execute(node.operator, inputs, cancel_event)

    async def _perform_and_commit(
        self,
        node: PhysicalNode,
        run_id: str,
        inputs: tuple[pa.Table, ...],
        cancel_event: asyncio.Event,
        memory: _MemoryBudget,
    ) -> tuple[pa.Table, CommittedManifest]:
        table = await self._perform_node(node, inputs, cancel_event)
        self.executor.enforce_limits(table)
        if cancel_event.is_set():
            raise asyncio.CancelledError
        await memory.reserve(run_id, node.id, table.nbytes)
        try:
            manifest = await self.result_store.commit(
                run_id, node.id, table, cancel_event
            )
            return table, manifest
        except DeferredCleanupCancellation as exc:
            await memory.defer(run_id, node.id)
            deferred = asyncio.create_task(
                self._release_after_cleanup(
                    exc.cleanup,
                    memory,
                    run_id,
                    node.id,
                )
            )
            self._deferred_releases.add(deferred)
            deferred.add_done_callback(self._finish_deferred_release)
            raise asyncio.CancelledError from None
        except asyncio.CancelledError:
            await memory.release(run_id, node.id)
            raise
        except Exception:
            await memory.release(run_id, node.id)
            raise

    async def _claim_run(
        self, run_id: str, cancel_event: asyncio.Event
    ) -> None:
        async with self._run_lock:
            if (
                run_id in self._known_run_ids
                or run_id in self._claiming_run_ids
            ):
                raise RuntimeFailure(
                    "RUN_ID_CONFLICT",
                    "Run ID has already been used by this coordinator",
                )
            self._claiming_run_ids.add(run_id)
        claim = asyncio.create_task(self.event_store.claim(run_id))
        was_cancelled = False
        while not claim.done():
            try:
                await asyncio.shield(claim)
            except asyncio.CancelledError:
                was_cancelled = True
        if claim.cancelled():
            async with self._run_lock:
                self._claiming_run_ids.discard(run_id)
            raise asyncio.CancelledError
        try:
            claimed = claim.result()
        except Exception:
            async with self._run_lock:
                self._claiming_run_ids.discard(run_id)
            raise
        if not claimed:
            async with self._run_lock:
                self._claiming_run_ids.discard(run_id)
            if was_cancelled:
                raise asyncio.CancelledError
            raise RuntimeFailure(
                "RUN_ID_CONFLICT",
                "Run ID has already been used by this coordinator",
            )
        async with self._run_lock:
            self._claiming_run_ids.discard(run_id)
            self._register_run(run_id, cancel_event)
        if was_cancelled:
            raise asyncio.CancelledError

    def _register_run(self, run_id: str, cancel_event: asyncio.Event) -> None:
        self._known_run_ids.add(run_id)
        self._cancellations[run_id] = cancel_event
        self._accepting_cancellation.add(run_id)

    async def _owns_run(
        self, run_id: str, cancel_event: asyncio.Event
    ) -> bool:
        async with self._run_lock:
            return self._cancellations.get(run_id) is cancel_event

    async def _release_after_cleanup(
        self,
        cleanup: asyncio.Task[None],
        memory: _MemoryBudget,
        run_id: str,
        node_id: str,
    ) -> None:
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        while True:
            try:
                await memory.release(run_id, node_id)
                break
            except asyncio.CancelledError:
                continue

    def _finish_deferred_release(self, task: asyncio.Task[None]) -> None:
        self._deferred_releases.discard(task)
        with suppress(asyncio.CancelledError, Exception):
            task.result()

    async def _release_consumed_tables(
        self,
        wave_nodes: list[PhysicalNode],
        remaining_consumers: dict[str, int],
        tables: dict[str, pa.Table],
        memory: _MemoryBudget,
        run_id: str,
    ) -> None:
        for node in wave_nodes:
            for dependency in node.dependencies:
                remaining_consumers[dependency] -= 1
        releasable = [
            node_id
            for node_id in tables
            if remaining_consumers[node_id] == 0
        ]
        for node_id in releasable:
            tables.pop(node_id, None)
            await memory.release(run_id, node_id)

    async def _finalize_outer_cancellation(
        self, plan: PhysicalPlan, emitter: _Emitter
    ) -> None:
        events = await self.event_store.list(emitter.run_id)
        terminal_nodes = {
            event.scope_id
            for event in events
            if event.scope == "node" and event.state in TERMINAL_STATES
        }
        terminal_stages = {
            event.scope_id
            for event in events
            if event.scope == "stage" and event.state in TERMINAL_STATES
        }
        for wave in sorted({node.wave for node in plan.nodes}):
            stage_id = f"wave-{wave}"
            if stage_id not in terminal_stages:
                await emitter.emit(
                    scope="stage",
                    scope_id=stage_id,
                    state=ExecutionState.CANCELLED,
                    code="STAGE_CANCELLED",
                    message="Stage cancelled by caller",
                    metadata={"wave": wave},
                )
            for node in sorted(
                (item for item in plan.nodes if item.wave == wave),
                key=lambda item: item.id,
            ):
                if node.id not in terminal_nodes:
                    await emitter.emit(
                        scope="node",
                        scope_id=node.id,
                        state=ExecutionState.CANCELLED,
                        code="NODE_CANCELLED",
                        message="Node cancelled by caller",
                        metadata={
                            "wave": wave,
                            "operation": node.operation.value,
                        },
                    )
        if not any(
            event.scope == "run" and event.state in TERMINAL_STATES
            for event in events
        ):
            await emitter.emit(
                scope="run",
                scope_id=emitter.run_id,
                state=ExecutionState.CANCELLED,
                code="RUN_CANCELLED",
                message="Run cancelled by caller",
            )

    async def _cancel_resolver(
        self, run_id: str, node_id: str, emitter: _Emitter
    ) -> None:
        timeout = min(1.0, self.limits.node_timeout_seconds)
        cancellation = asyncio.create_task(self.resolver.cancel(run_id, node_id))
        try:
            done, _ = await asyncio.wait(
                {cancellation},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            cancellation.cancel()
            cancellation.add_done_callback(_consume_task_result)
            code = "RESOLVER_CANCEL_INTERRUPTED"
        else:
            if cancellation not in done:
                cancellation.cancel()
                cancellation.add_done_callback(_consume_task_result)
                code = "RESOLVER_CANCEL_TIMEOUT"
            else:
                try:
                    cancellation.result()
                except asyncio.CancelledError:
                    code = "RESOLVER_CANCEL_INTERRUPTED"
                except Exception:
                    code = "RESOLVER_CANCEL_FAILED"
                else:
                    return
        await emitter.emit(
            scope="node",
            scope_id=node_id,
            state=ExecutionState.RUNNING,
            code=code,
            message="Source cancellation did not complete cleanly",
        )

    async def _terminalize_waves(
        self,
        nodes_by_wave: dict[int, list[PhysicalNode]],
        *,
        first_wave: int,
        emitter: _Emitter,
        state: ExecutionState,
    ) -> None:
        for wave in sorted(item for item in nodes_by_wave if item >= first_wave):
            stage_id = f"wave-{wave}"
            stage_machine = StateMachine()
            stage_machine.transition(state)
            await emitter.emit(
                scope="stage",
                scope_id=stage_id,
                state=state,
                code=f"STAGE_{state.value}",
                message=f"Stage {state.value.lower()} before execution",
                metadata={"wave": wave},
            )
            await self._skip_nodes(
                sorted(nodes_by_wave[wave], key=lambda node: node.id),
                emitter,
                state,
            )

    async def _skip_nodes(
        self,
        nodes: list[PhysicalNode],
        emitter: _Emitter,
        state: ExecutionState,
    ) -> None:
        for node in nodes:
            machine = StateMachine()
            machine.transition(state)
            await emitter.emit(
                scope="node",
                scope_id=node.id,
                state=state,
                code=f"NODE_{state.value}",
                message=f"Node {state.value.lower()} before execution",
                metadata={"wave": node.wave, "operation": node.operation.value},
            )


def validate_physical_plan(plan: PhysicalPlan) -> None:
    by_id = {node.id: node for node in plan.nodes}
    if len(by_id) != len(plan.nodes):
        raise PlanFailure("PLAN_DUPLICATE_NODE", "Physical plan contains duplicate IDs")
    if plan.output_node_id not in by_id:
        raise PlanFailure("PLAN_OUTPUT_MISSING", "Physical plan output node does not exist")
    for node in plan.nodes:
        for dependency in node.dependencies:
            if dependency not in by_id:
                raise PlanFailure(
                    "PLAN_MISSING_DEPENDENCY",
                    f"Node '{node.id}' references missing dependency '{dependency}'",
                )
            if by_id[dependency].wave >= node.wave:
                raise PlanFailure(
                    "PLAN_WAVE_INVALID",
                    "Dependency waves must precede dependent node waves",
                )
    indegree = {node.id: len(node.dependencies) for node in plan.nodes}
    dependents: dict[str, list[str]] = defaultdict(list)
    for node in plan.nodes:
        for dependency in node.dependencies:
            dependents[dependency].append(node.id)
    ready = sorted(node_id for node_id, count in indegree.items() if count == 0)
    visited = 0
    while ready:
        node_id = ready.pop(0)
        visited += 1
        for child in sorted(dependents[node_id]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if visited != len(plan.nodes):
        raise PlanFailure("PLAN_CYCLE", "Physical plan contains a dependency cycle")


def _terminal_stage_state(outcomes: list[_NodeOutcome]) -> ExecutionState:
    states = {outcome.state for outcome in outcomes}
    if ExecutionState.TIMED_OUT in states:
        return ExecutionState.TIMED_OUT
    if ExecutionState.FAILED in states:
        return ExecutionState.FAILED
    if ExecutionState.CANCELLED in states:
        return ExecutionState.CANCELLED
    return ExecutionState.SUCCEEDED


def _duration(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _consume_task_result(task: asyncio.Task[None]) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.result()
