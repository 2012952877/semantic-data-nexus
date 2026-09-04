"""Bounded, observable DAG coordinator for validated physical plans."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from query_runtime.arrow_memory import BufferIdentity, retained_buffer_sizes
from query_runtime.domain import (
    TERMINAL_STATES,
    CommittedManifest,
    DiagnosticEvent,
    ExecutionState,
    LineageGraph,
    OperatorKind,
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
from query_runtime.resolver import ExecutionContext, SourceResolver
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
        self._reservation_buffers: dict[
            tuple[str, str], frozenset[BufferIdentity]
        ] = {}
        self._buffer_sizes: dict[BufferIdentity, int] = {}
        self._buffer_references: dict[BufferIdentity, int] = {}
        self._deferred: set[tuple[str, str]] = set()
        self._retainers: dict[tuple[str, str], int] = {}
        self._release_pending: set[tuple[str, str]] = set()
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

    async def reconcile(
        self, run_id: str, node_id: str, table: pa.Table
    ) -> None:
        buffers = retained_buffer_sizes(table)
        size = sum(buffers.values())
        async with self._lock:
            key = (run_id, node_id)
            reserved = self._reservations[key]
            if size > reserved:
                raise ResourceLimitFailure(
                    "LIMIT_ADMISSION_ESTIMATE_EXCEEDED",
                    "Node output exceeded its pre-admitted memory reservation",
                    details={"reserved_bytes": reserved, "actual_bytes": size},
                )
            self.current -= reserved
            self._reservations[key] = size
            identities = frozenset(buffers)
            self._reservation_buffers[key] = identities
            for identity, buffer_size in buffers.items():
                references = self._buffer_references.get(identity, 0)
                if references == 0:
                    self.current += buffer_size
                    self._buffer_sizes[identity] = buffer_size
                self._buffer_references[identity] = references + 1

    async def release(self, run_id: str, node_id: str) -> None:
        async with self._lock:
            key = (run_id, node_id)
            if self._retainers.get(key, 0) > 0:
                self._release_pending.add(key)
                return
            self._release_key(key)

    async def defer(self, run_id: str, node_id: str) -> None:
        async with self._lock:
            key = (run_id, node_id)
            if key in self._reservations:
                self._deferred.add(key)

    async def retain(self, run_id: str, node_id: str) -> None:
        async with self._lock:
            key = (run_id, node_id)
            if key not in self._reservations:
                raise RuntimeFailure(
                    "MEMORY_RESERVATION_MISSING",
                    "Cannot retain an input without an active memory reservation",
                    details={"node_id": node_id},
                )
            self._retainers[key] = self._retainers.get(key, 0) + 1

    async def release_retained(self, run_id: str, node_id: str) -> None:
        async with self._lock:
            key = (run_id, node_id)
            retained = self._retainers.get(key, 0)
            if retained <= 1:
                self._retainers.pop(key, None)
                if key in self._release_pending:
                    self._release_key(key)
                return
            self._retainers[key] = retained - 1

    async def release_run(self, run_id: str) -> None:
        async with self._lock:
            retained = {
                key
                for key in self._reservations
                if key[0] == run_id and self._retainers.get(key, 0) > 0
            }
            self._release_pending.update(retained)
            keys = [
                key
                for key in self._reservations
                if key[0] == run_id
                and key not in self._deferred
                and key not in retained
            ]
            for key in keys:
                self._release_key(key)

    def _release_key(self, key: tuple[str, str]) -> None:
        reserved = self._reservations.pop(key, 0)
        identities = self._reservation_buffers.pop(key, frozenset())
        if identities:
            for identity in identities:
                references = self._buffer_references[identity] - 1
                if references == 0:
                    self.current -= self._buffer_sizes.pop(identity)
                    self._buffer_references.pop(identity)
                else:
                    self._buffer_references[identity] = references
        else:
            self.current -= reserved
        self._deferred.discard(key)
        self._release_pending.discard(key)
        self.current = max(0, self.current)


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
        was_cancelled = False
        async with self._lock:
            sequence = self._sequence
            persistence = asyncio.create_task(
                self.store.append(
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
            )
            was_cancelled = await _wait_for_task_completion(persistence)
            persistence.result()
            self._sequence += 1
        if was_cancelled:
            raise asyncio.CancelledError


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
        self._node_cleanups: set[asyncio.Task[None]] = set()
        self._resolver_cancellations: set[asyncio.Task[None]] = set()

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
                        if item.state is stage_state
                        and item.diagnostic_code is not None
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
        cleanup_deferred = False
        resolver_cancel_requested = False
        execution: asyncio.Task[tuple[pa.Table, CommittedManifest]] | None = None
        inputs: tuple[pa.Table, ...] = ()
        execution_context = ExecutionContext(
            run_id=run_id,
            node_id=node.id,
            attempt=1,
            cancellation_handle=f"exec-{uuid.uuid4().hex}",
        )
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
                execution = asyncio.create_task(
                    self._perform_and_commit(
                        node,
                        run_id,
                        inputs,
                        cancel_event,
                        memory,
                        execution_context,
                    )
                )
                cancellation = asyncio.create_task(cancel_event.wait())
                try:
                    done, _ = await asyncio.wait(
                        {execution, cancellation},
                        timeout=self.limits.node_timeout_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not cancellation.done():
                        cancellation.cancel()
                        with suppress(asyncio.CancelledError):
                            await cancellation
                if cancellation in done and execution not in done:
                    if (
                        node.kind is PhysicalNodeKind.SOURCE_FRAGMENT
                        and not resolver_cancel_requested
                    ):
                        self._request_resolver_cancel(execution_context)
                        resolver_cancel_requested = True
                    await self._handoff_deferred_execution(
                        execution,
                        memory,
                        run_id,
                        node.id,
                        node.dependencies,
                        inputs,
                    )
                    cleanup_deferred = True
                    raise asyncio.CancelledError
                if execution not in done:
                    cancel_event.set()
                    was_cancelled = await self._handoff_deferred_execution(
                        execution,
                        memory,
                        run_id,
                        node.id,
                        node.dependencies,
                        inputs,
                    )
                    cleanup_deferred = True
                    if node.kind is PhysicalNodeKind.SOURCE_FRAGMENT:
                        self._request_resolver_cancel(execution_context)
                        resolver_cancel_requested = True
                    if was_cancelled:
                        raise asyncio.CancelledError
                    diagnostic_code = await self._emit_node_terminal(
                        machine,
                        emitter,
                        scope="node",
                        scope_id=node.id,
                        state=ExecutionState.TIMED_OUT,
                        code="NODE_TIMEOUT",
                        message="Node exceeded its configured execution time",
                        duration_ms=_duration(started),
                    )
                    return _NodeOutcome(
                        node.id,
                        machine.state,
                        diagnostic_code=diagnostic_code,
                    )
                table, manifest = execution.result()
                reserved = True
                if cancel_event.is_set():
                    await memory.release(run_id, node.id)
                    reserved = False
                    raise asyncio.CancelledError
                diagnostic_code = await self._emit_node_terminal(
                    machine,
                    emitter,
                    scope="node",
                    scope_id=node.id,
                    state=ExecutionState.SUCCEEDED,
                    code="NODE_SUCCEEDED",
                    message="Node succeeded",
                    duration_ms=_duration(started),
                    metadata={
                        "wave": node.wave,
                        "operation": node.operation.value,
                        "rows": table.num_rows,
                    },
                )
                if diagnostic_code != "NODE_SUCCEEDED":
                    await memory.release(run_id, node.id)
                    reserved = False
                    cancel_event.set()
                    return _NodeOutcome(
                        node.id,
                        machine.state,
                        diagnostic_code=diagnostic_code,
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
            if (
                node.kind is PhysicalNodeKind.SOURCE_FRAGMENT
                and not resolver_cancel_requested
            ):
                self._request_resolver_cancel(execution_context)
            diagnostic_code = await self._emit_node_terminal(
                machine,
                emitter,
                scope="node",
                scope_id=node.id,
                state=ExecutionState.TIMED_OUT,
                code="NODE_TIMEOUT",
                message="Node exceeded its configured execution time",
                duration_ms=_duration(started),
            )
            return _NodeOutcome(
                node.id, machine.state, diagnostic_code=diagnostic_code
            )
        except asyncio.CancelledError:
            if machine.state in TERMINAL_STATES:
                raise
            if (
                execution is not None
                and not execution.done()
                and not cleanup_deferred
            ):
                await self._handoff_deferred_execution(
                    execution,
                    memory,
                    run_id,
                    node.id,
                    node.dependencies,
                    inputs,
                )
                cleanup_deferred = True
            if reserved:
                await memory.release(run_id, node.id)
            cancel_event.set()
            if (
                node.kind is PhysicalNodeKind.SOURCE_FRAGMENT
                and not resolver_cancel_requested
            ):
                self._request_resolver_cancel(execution_context)
            diagnostic_code = await self._emit_node_terminal(
                machine,
                emitter,
                scope="node",
                scope_id=node.id,
                state=ExecutionState.CANCELLED,
                code="NODE_CANCELLED",
                message="Node was cancelled",
                duration_ms=_duration(started),
            )
            return _NodeOutcome(
                node.id, machine.state, diagnostic_code=diagnostic_code
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
            diagnostic_code = await self._emit_node_terminal(
                machine,
                emitter,
                scope="node",
                scope_id=node.id,
                state=ExecutionState.FAILED,
                code=code,
                message=message,
                duration_ms=_duration(started),
            )
            return _NodeOutcome(
                node.id, machine.state, diagnostic_code=diagnostic_code
            )

    async def _emit_node_terminal(
        self,
        machine: StateMachine,
        emitter: _Emitter,
        *,
        scope: str,
        scope_id: str,
        state: ExecutionState,
        code: str,
        message: str,
        duration_ms: int | None = None,
        metadata: dict[str, str | int | bool | None] | None = None,
    ) -> str:
        try:
            await self._persist_node_terminal(
                machine,
                emitter,
                scope=scope,
                scope_id=scope_id,
                state=state,
                code=code,
                message=message,
                duration_ms=duration_ms,
                metadata=metadata,
            )
            return code
        except RuntimeFailure as exc:
            if exc.code != "EVENT_STORE_WRITE_FAILED":
                raise
        await self._persist_node_terminal(
            machine,
            emitter,
            scope=scope,
            scope_id=scope_id,
            state=ExecutionState.FAILED,
            code="EVENT_STORE_WRITE_FAILED",
            message="Node terminal event could not be persisted",
            duration_ms=duration_ms,
            metadata=metadata,
        )
        return "EVENT_STORE_WRITE_FAILED"

    async def _persist_node_terminal(
        self,
        machine: StateMachine,
        emitter: _Emitter,
        *,
        scope: str,
        scope_id: str,
        state: ExecutionState,
        code: str,
        message: str,
        duration_ms: int | None = None,
        metadata: dict[str, str | int | bool | None] | None = None,
    ) -> None:
        machine.validate_transition(state)
        emission = asyncio.create_task(
            emitter.emit(
                scope=scope,
                scope_id=scope_id,
                state=state,
                code=code,
                message=message,
                duration_ms=duration_ms,
                metadata=metadata,
            )
        )
        was_cancelled = await _wait_for_task_completion(emission)
        try:
            emission.result()
        except Exception as exc:
            raise RuntimeFailure(
                "EVENT_STORE_WRITE_FAILED",
                "Node terminal event could not be persisted",
            ) from exc
        machine.transition(state)
        if was_cancelled:
            raise asyncio.CancelledError

    async def _perform_node(
        self,
        node: PhysicalNode,
        inputs: tuple[pa.Table, ...],
        cancel_event: asyncio.Event,
        execution_context: ExecutionContext,
    ) -> pa.Table:
        if node.kind is PhysicalNodeKind.SOURCE_FRAGMENT:
            assert node.source_fragment is not None
            return await self.resolver.execute(
                execution_context, node.source_fragment, cancel_event
            )
        assert node.operator is not None
        return await self.executor.execute(node.operator, inputs, cancel_event)

    async def _perform_and_commit(
        self,
        node: PhysicalNode,
        run_id: str,
        inputs: tuple[pa.Table, ...],
        cancel_event: asyncio.Event,
        memory: _MemoryBudget,
        execution_context: ExecutionContext,
    ) -> tuple[pa.Table, CommittedManifest]:
        await memory.reserve(run_id, node.id, self.limits.max_bytes)
        try:
            table = await self._perform_node(
                node, inputs, cancel_event, execution_context
            )
            self.executor.enforce_limits(table)
            await memory.reconcile(run_id, node.id, table)
            if cancel_event.is_set():
                raise asyncio.CancelledError
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
        registered = False
        was_cancelled = False
        pending_error: BaseException | None = None
        try:
            was_cancelled = await _wait_for_task_completion(claim)
            if claim.cancelled():
                raise asyncio.CancelledError
            try:
                claimed = claim.result()
            except Exception:
                if was_cancelled:
                    raise asyncio.CancelledError from None
                raise
            if not claimed:
                if was_cancelled:
                    raise asyncio.CancelledError
                raise RuntimeFailure(
                    "RUN_ID_CONFLICT",
                    "Run ID has already been used by this coordinator",
                )
            settlement = asyncio.create_task(
                self._settle_claim(run_id, cancel_event, register=True)
            )
            was_cancelled = (
                await _wait_for_task_completion(settlement)
                or was_cancelled
            )
            settlement.result()
            registered = True
            if was_cancelled:
                raise asyncio.CancelledError
        except BaseException as exc:
            pending_error = exc
        finally:
            if not registered:
                settlement = asyncio.create_task(
                    self._settle_claim(run_id, cancel_event, register=False)
                )
                was_cancelled = (
                    await _wait_for_task_completion(settlement)
                    or was_cancelled
                )
                settlement.result()
        if was_cancelled:
            raise asyncio.CancelledError from None
        if pending_error is not None:
            raise pending_error

    async def _settle_claim(
        self,
        run_id: str,
        cancel_event: asyncio.Event,
        *,
        register: bool,
    ) -> None:
        async with self._run_lock:
            self._claiming_run_ids.discard(run_id)
            if register:
                self._register_run(run_id, cancel_event)

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
        try:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        finally:
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

    def _defer_node_cleanup(
        self,
        execution: asyncio.Task[tuple[pa.Table, CommittedManifest]],
        memory: _MemoryBudget,
        run_id: str,
        node_id: str,
        dependency_ids: tuple[str, ...],
        retained_inputs: tuple[pa.Table, ...],
    ) -> None:
        cleanup = asyncio.create_task(
            self._observe_node_cleanup(
                execution,
                memory,
                run_id,
                node_id,
                dependency_ids,
                retained_inputs,
            )
        )
        self._node_cleanups.add(cleanup)
        cleanup.add_done_callback(self._finish_node_cleanup)

    async def _handoff_deferred_execution(
        self,
        execution: asyncio.Task[tuple[pa.Table, CommittedManifest]],
        memory: _MemoryBudget,
        run_id: str,
        node_id: str,
        dependency_ids: tuple[str, ...],
        retained_inputs: tuple[pa.Table, ...],
    ) -> bool:
        handoff = asyncio.create_task(
            self._prepare_deferred_execution(
                execution,
                memory,
                run_id,
                node_id,
                dependency_ids,
                retained_inputs,
            )
        )
        was_cancelled = await _wait_for_task_completion(handoff)
        handoff.result()
        return was_cancelled

    async def _prepare_deferred_execution(
        self,
        execution: asyncio.Task[tuple[pa.Table, CommittedManifest]],
        memory: _MemoryBudget,
        run_id: str,
        node_id: str,
        dependency_ids: tuple[str, ...],
        retained_inputs: tuple[pa.Table, ...],
    ) -> None:
        retained: list[str] = []
        try:
            await memory.defer(run_id, node_id)
            for dependency in dependency_ids:
                await memory.retain(run_id, dependency)
                retained.append(dependency)
        except BaseException:
            for dependency in reversed(retained):
                await memory.release_retained(run_id, dependency)
            execution.cancel()
            self._defer_node_cleanup(
                execution, memory, run_id, node_id, (), ()
            )
            raise
        execution.cancel()
        self._defer_node_cleanup(
            execution,
            memory,
            run_id,
            node_id,
            tuple(retained),
            retained_inputs,
        )

    async def _observe_node_cleanup(
        self,
        execution: asyncio.Task[tuple[pa.Table, CommittedManifest]],
        memory: _MemoryBudget,
        run_id: str,
        node_id: str,
        dependency_ids: tuple[str, ...],
        retained_inputs: tuple[pa.Table, ...],
    ) -> None:
        done, _ = await asyncio.wait(
            {execution},
            timeout=min(1.0, self.limits.node_timeout_seconds),
        )
        if execution not in done:
            execution.cancel()
            execution.add_done_callback(
                lambda task: self._schedule_late_execution_release(
                    task,
                    memory,
                    run_id,
                    node_id,
                    dependency_ids,
                    retained_inputs,
                )
            )
            return
        try:
            execution.result()
        except (asyncio.CancelledError, Exception):
            pass
        else:
            await memory.release(run_id, node_id)
        finally:
            for dependency in dependency_ids:
                await memory.release_retained(run_id, dependency)

    def _schedule_late_execution_release(
        self,
        execution: asyncio.Task[tuple[pa.Table, CommittedManifest]],
        memory: _MemoryBudget,
        run_id: str,
        node_id: str,
        dependency_ids: tuple[str, ...],
        retained_inputs: tuple[pa.Table, ...],
    ) -> None:
        loop = execution.get_loop()
        if loop.is_closed():
            return
        release = loop.create_task(
            self._release_late_execution(
                execution,
                memory,
                run_id,
                node_id,
                dependency_ids,
                retained_inputs,
            )
        )
        self._deferred_releases.add(release)
        release.add_done_callback(self._finish_deferred_release)

    async def _release_late_execution(
        self,
        execution: asyncio.Task[tuple[pa.Table, CommittedManifest]],
        memory: _MemoryBudget,
        run_id: str,
        node_id: str,
        dependency_ids: tuple[str, ...],
        retained_inputs: tuple[pa.Table, ...],
    ) -> None:
        try:
            execution.result()
        except (asyncio.CancelledError, Exception):
            pass
        else:
            await memory.release(run_id, node_id)
        finally:
            for dependency in dependency_ids:
                await memory.release_retained(run_id, dependency)

    def _finish_node_cleanup(self, task: asyncio.Task[None]) -> None:
        self._node_cleanups.discard(task)
        with suppress(asyncio.CancelledError, Exception):
            task.result()

    def _request_resolver_cancel(self, context: ExecutionContext) -> None:
        cancellation = asyncio.create_task(
            self._bounded_resolver_cancel(context.cancellation_handle)
        )
        self._resolver_cancellations.add(cancellation)
        cancellation.add_done_callback(self._finish_resolver_cancel)

    async def _bounded_resolver_cancel(
        self, cancellation_handle: str
    ) -> None:
        cancellation = asyncio.create_task(
            self.resolver.cancel(cancellation_handle)
        )
        done, _ = await asyncio.wait(
            {cancellation},
            timeout=min(1.0, self.limits.node_timeout_seconds),
        )
        if cancellation not in done:
            cancellation.cancel()
            cancellation.add_done_callback(_consume_task_result)
            return
        _consume_task_result(cancellation)

    def _finish_resolver_cancel(self, task: asyncio.Task[None]) -> None:
        self._resolver_cancellations.discard(task)
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
        if node.kind is PhysicalNodeKind.SOURCE_FRAGMENT and node.dependencies:
            raise PlanFailure(
                "PLAN_SOURCE_FRAGMENT_INPUT",
                f"Source fragment node '{node.id}' cannot accept physical inputs",
                details={"node_id": node.id, "actual_inputs": len(node.dependencies)},
            )
        if node.kind is PhysicalNodeKind.OPERATOR:
            required_inputs = 2 if node.operation is OperatorKind.JOIN else 1
            if len(node.dependencies) != required_inputs:
                raise PlanFailure(
                    "PLAN_OPERATOR_INPUT_COUNT",
                    f"Operator node '{node.id}' requires {required_inputs} input(s)",
                    details={
                        "node_id": node.id,
                        "operation": node.operation.value,
                        "required_inputs": required_inputs,
                        "actual_inputs": len(node.dependencies),
                    },
                )
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
    reachable: set[str] = set()
    pending = [plan.output_node_id]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(by_id[node_id].dependencies)
    unreachable = sorted(set(by_id) - reachable)
    if unreachable:
        raise PlanFailure(
            "PLAN_UNREACHABLE_NODE",
            "Physical plan contains nodes outside the output dependency closure",
            details={"nodes": unreachable},
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


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.result()


async def _wait_for_task_completion(task: asyncio.Task[Any]) -> bool:
    was_cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            was_cancelled = True
        except Exception:
            pass
    return was_cancelled
