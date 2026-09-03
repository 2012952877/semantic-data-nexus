from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

import pyarrow as pa
import pytest

from query_runtime.coordinator import QueryCoordinator, validate_physical_plan
from query_runtime.domain import (
    BoundSource,
    CapabilityCatalog,
    CommittedManifest,
    DiagnosticEvent,
    ExecutionState,
    OperatorKind,
    OperatorSpec,
    PhysicalNode,
    PhysicalNodeKind,
    PhysicalPlan,
    SourceFragment,
)
from query_runtime.errors import PlanFailure, RuntimeFailure
from query_runtime.events import InMemoryEventStore
from query_runtime.fixtures import (
    complex_profit_fixture,
    delayed_fixture,
    invalid_dag_fixture,
    join_sort_fixture,
    local_operator_failure_fixture,
    simple_profit_fixture,
    source_failure_fixture,
    zero_rows_fixture,
)
from query_runtime.operators import ResourceLimits
from query_runtime.resolver import FakeResolver
from query_runtime.result_store import InlineResultStore, ParquetResultStore


class _PausingEventStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.final_stage = asyncio.Event()
        self.release = asyncio.Event()

    async def append(self, event: DiagnosticEvent) -> None:
        if event.code == "STAGE_SUCCEEDED":
            self.final_stage.set()
            await self.release.wait()
        await super().append(event)


class _PausingNodeTerminalEventStore(InMemoryEventStore):
    def __init__(self, terminal_code: str) -> None:
        super().__init__()
        self.terminal_code = terminal_code
        self.node_terminal = asyncio.Event()
        self.release = asyncio.Event()

    async def append(self, event: DiagnosticEvent) -> None:
        if event.code == self.terminal_code:
            self.node_terminal.set()
            await self.release.wait()
        await super().append(event)


class _PausingClaimStore(InMemoryEventStore):
    def __init__(self, pause_run_id: str = "run-claim-cancel") -> None:
        super().__init__()
        self.pause_run_id = pause_run_id
        self.claimed = asyncio.Event()
        self.release = asyncio.Event()

    async def claim(self, run_id: str) -> bool:
        result = await super().claim(run_id)
        if run_id == self.pause_run_id:
            self.claimed.set()
            await self.release.wait()
        return result


class _SlowParquetStore(ParquetResultStore):
    def _write_temporary(
        self,
        temporary: Path,
        final: Path,
        result_id: str,
        run_id: str,
        node_id: str,
        table: pa.Table,
    ) -> CommittedManifest:
        time.sleep(0.2)
        return super()._write_temporary(
            temporary, final, result_id, run_id, node_id, table
        )


class _FailingSlowCleanupStore(_SlowParquetStore):
    def _remove_temporary(self, temporary: Path) -> None:
        raise OSError("synthetic cleanup failure")


class _CancellingClaimStore(InMemoryEventStore):
    async def claim(self, run_id: str) -> bool:
        raise asyncio.CancelledError


def _memory_chain_fixture() -> tuple[PhysicalPlan, FakeResolver, int]:
    alias = "memory-source"
    source = PhysicalNode(
        id="memory-source",
        kind=PhysicalNodeKind.SOURCE_FRAGMENT,
        operation=OperatorKind.SOURCE,
        wave=0,
        logical_node_ids=("logical-source",),
        source_fragment=SourceFragment(
            source=BoundSource(
                alias=alias, source_type="synthetic", object_name="memory_facts"
            ),
            operations=(OperatorSpec(kind=OperatorKind.SOURCE),),
        ),
    )
    nodes = [source]
    dependency = source.id
    for index in range(3):
        node = PhysicalNode(
            id=f"project-{index}",
            kind=PhysicalNodeKind.OPERATOR,
            operation=OperatorKind.PROJECT,
            dependencies=(dependency,),
            wave=index + 1,
            logical_node_ids=(f"logical-project-{index}",),
            operator=OperatorSpec(kind=OperatorKind.PROJECT, columns=("value",)),
        )
        nodes.append(node)
        dependency = node.id
    table = pa.table({"value": list(range(1_000))})
    resolver = FakeResolver(
        tables={alias: table},
        catalogs={
            alias: CapabilityCatalog(
                source_alias=alias,
                source_type="synthetic",
                operator_kinds=frozenset({OperatorKind.SOURCE}),
            )
        },
    )
    return (
        PhysicalPlan(
            id="memory-chain",
            nodes=tuple(nodes),
            output_node_id=nodes[-1].id,
        ),
        resolver,
        table.nbytes,
    )


async def _wait_for_run_started(
    coordinator: QueryCoordinator, run_id: str
) -> None:
    for _ in range(100):
        if any(
            event.code == "RUN_STARTED"
            for event in await coordinator.event_store.list(run_id)
        ):
            return
        await asyncio.sleep(0)
    raise AssertionError(f"run '{run_id}' did not start")


async def _wait_for_coordinator_cleanup(coordinator: QueryCoordinator) -> None:
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not coordinator._node_cleanups and not coordinator._deferred_releases:
            return
    raise AssertionError("coordinator background cleanup did not settle")


@pytest.mark.asyncio
async def test_complex_fixture_commits_final_manifest_and_events(tmp_path: Path) -> None:
    fixture = complex_profit_fixture()
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=ParquetResultStore(tmp_path),
    )
    outcome = await coordinator.run(fixture.plan, run_id="run-complex")
    assert outcome.summary.state is ExecutionState.SUCCEEDED
    assert outcome.manifest is not None
    assert outcome.manifest.row_count == 2
    assert (Path(outcome.manifest.result.uri) / "_COMMITTED").is_file()
    assert [event.sequence for event in outcome.events] == list(range(len(outcome.events)))
    assert {event.scope for event in outcome.events} == {"run", "stage", "node"}


@pytest.mark.asyncio
async def test_source_failure_and_local_failure_never_return_success(
    tmp_path: Path,
) -> None:
    source_fixture = source_failure_fixture()
    failed = await QueryCoordinator(
        resolver=source_fixture.resolver,
        result_store=ParquetResultStore(tmp_path / "source"),
    ).run(source_fixture.plan, run_id="run-source-fail")
    assert failed.summary.state is ExecutionState.FAILED
    assert failed.summary.result is None
    assert failed.manifest is None
    source_failure_events = [
        event for event in failed.events if event.code == "RESOLVER_SOURCE_FAILED"
    ]
    assert source_failure_events[0].message == "Source resolver failed"

    local_fixture = local_operator_failure_fixture()
    local_failed = await QueryCoordinator(
        resolver=local_fixture.resolver,
        result_store=ParquetResultStore(tmp_path / "local"),
    ).run(local_fixture.plan, run_id="run-local-fail")
    assert local_failed.summary.state is ExecutionState.FAILED
    assert local_failed.summary.result is None
    assert local_failed.summary.diagnostic_code == "COLUMN_MISSING"


@pytest.mark.asyncio
async def test_source_results_obey_runtime_resource_limits(tmp_path: Path) -> None:
    fixture = simple_profit_fixture()
    outcome = await QueryCoordinator(
        resolver=fixture.resolver,
        result_store=ParquetResultStore(tmp_path),
        limits=ResourceLimits(max_rows=1),
    ).run(fixture.plan, run_id="run-row-limit")
    assert outcome.summary.state is ExecutionState.FAILED
    assert outcome.summary.diagnostic_code == "LIMIT_ROWS_EXCEEDED"
    assert outcome.manifest is None


@pytest.mark.asyncio
async def test_cancel_and_timeout_propagate(tmp_path: Path) -> None:
    fixture = delayed_fixture()
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=ParquetResultStore(tmp_path / "cancel"),
    )
    task = asyncio.create_task(coordinator.run(fixture.plan, run_id="run-cancel"))
    await asyncio.sleep(0.03)
    assert await coordinator.cancel("run-cancel")
    cancelled = await task
    assert cancelled.summary.state is ExecutionState.CANCELLED
    assert cancelled.manifest is None
    assert fixture.resolver.cancelled == [("run-cancel", "source-profit")]

    timeout_fixture = delayed_fixture()
    timed_out = await QueryCoordinator(
        resolver=timeout_fixture.resolver,
        result_store=ParquetResultStore(tmp_path / "timeout"),
        limits=ResourceLimits(node_timeout_seconds=0.01),
    ).run(timeout_fixture.plan, run_id="run-timeout")
    assert timed_out.summary.state is ExecutionState.TIMED_OUT
    assert timed_out.summary.diagnostic_code == "NODE_TIMEOUT"
    assert timed_out.manifest is None


@pytest.mark.asyncio
async def test_cancel_terminalizes_all_waves_when_resolver_cancel_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = complex_profit_fixture()
    fixture.resolver._delays["regional_source"] = 1.0

    async def failing_cancel(run_id: str, node_id: str) -> None:
        raise RuntimeError("synthetic resolver cancellation failure")

    monkeypatch.setattr(fixture.resolver, "cancel", failing_cancel)
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=ParquetResultStore(tmp_path),
    )
    task = asyncio.create_task(coordinator.run(fixture.plan, run_id="run-all-cancel"))
    await asyncio.sleep(0.03)
    assert await coordinator.cancel("run-all-cancel")
    outcome = await task
    assert outcome.summary.state is ExecutionState.CANCELLED
    terminal_nodes = {
        event.scope_id
        for event in outcome.events
        if event.scope == "node"
        and event.state
        in {
            ExecutionState.CANCELLED,
            ExecutionState.SKIPPED,
            ExecutionState.FAILED,
            ExecutionState.TIMED_OUT,
            ExecutionState.SUCCEEDED,
        }
    }
    assert terminal_nodes == {node.id for node in fixture.plan.nodes}
    terminal_stages = {
        event.scope_id
        for event in outcome.events
        if event.scope == "stage"
        and event.state
        in {
            ExecutionState.CANCELLED,
            ExecutionState.SKIPPED,
            ExecutionState.FAILED,
            ExecutionState.TIMED_OUT,
            ExecutionState.SUCCEEDED,
        }
    }
    assert terminal_stages == {f"wave-{wave}" for wave in range(4)}
    assert any(event.code == "RUN_CANCELLED" for event in outcome.events)


@pytest.mark.asyncio
async def test_late_accepted_cancellation_cannot_return_success(tmp_path: Path) -> None:
    fixture = simple_profit_fixture()
    events = _PausingEventStore()
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=ParquetResultStore(tmp_path),
        event_store=events,
    )
    task = asyncio.create_task(coordinator.run(fixture.plan, run_id="run-late-cancel"))
    await events.final_stage.wait()
    assert await coordinator.cancel("run-late-cancel")
    events.release.set()
    outcome = await task
    assert outcome.summary.state is ExecutionState.CANCELLED
    assert outcome.manifest is None
    assert any(event.code == "RUN_CANCELLED" for event in outcome.events)


@pytest.mark.asyncio
async def test_resolver_cancel_timeout_does_not_block_run_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = delayed_fixture()

    async def stubborn_cancel(run_id: str, node_id: str) -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)

    monkeypatch.setattr(fixture.resolver, "cancel", stubborn_cancel)
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=ParquetResultStore(tmp_path),
        limits=ResourceLimits(
            max_bytes=100_000,
            max_in_flight_bytes=100_000,
            node_timeout_seconds=0.02,
        ),
    )
    task = asyncio.create_task(
        coordinator.run(fixture.plan, run_id="run-stubborn-cancel")
    )
    await _wait_for_run_started(coordinator, "run-stubborn-cancel")
    started = asyncio.get_running_loop().time()
    assert await coordinator.cancel("run-stubborn-cancel")
    outcome = await task
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.1
    assert outcome.summary.state is ExecutionState.CANCELLED
    await asyncio.sleep(0.25)


@pytest.mark.parametrize("value", (True, 1.5, math.nan, math.inf))
def test_max_concurrency_requires_positive_integer(
    value: object, tmp_path: Path
) -> None:
    fixture = simple_profit_fixture()
    with pytest.raises(ValueError, match="positive integer"):
        QueryCoordinator(
            resolver=fixture.resolver,
            result_store=ParquetResultStore(tmp_path),
            max_concurrency=value,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_duplicate_and_reused_run_ids_are_rejected(tmp_path: Path) -> None:
    fixture = delayed_fixture()
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=ParquetResultStore(tmp_path),
    )
    first = asyncio.create_task(coordinator.run(fixture.plan, run_id="run-unique"))
    await _wait_for_run_started(coordinator, "run-unique")
    with pytest.raises(RuntimeFailure) as active:
        await coordinator.run(fixture.plan, run_id="run-unique")
    assert active.value.code == "RUN_ID_CONFLICT"
    assert await coordinator.cancel("run-unique")
    await first
    with pytest.raises(RuntimeFailure) as reused:
        await coordinator.run(fixture.plan, run_id="run-unique")
    assert reused.value.code == "RUN_ID_CONFLICT"


@pytest.mark.asyncio
async def test_shared_event_store_claims_run_id_across_coordinators() -> None:
    events = InMemoryEventStore()
    first_fixture = simple_profit_fixture()
    second_fixture = simple_profit_fixture()
    first = QueryCoordinator(
        resolver=first_fixture.resolver,
        result_store=InlineResultStore(),
        event_store=events,
    )
    second = QueryCoordinator(
        resolver=second_fixture.resolver,
        result_store=InlineResultStore(),
        event_store=events,
    )
    results = await asyncio.gather(
        first.run(first_fixture.plan, run_id="shared-run"),
        second.run(second_fixture.plan, run_id="shared-run"),
        return_exceptions=True,
    )
    conflicts = [
        result
        for result in results
        if isinstance(result, RuntimeFailure)
        and result.code == "RUN_ID_CONFLICT"
    ]
    successes = [
        result
        for result in results
        if not isinstance(result, BaseException)
        and result.summary.state is ExecutionState.SUCCEEDED
    ]
    assert len(conflicts) == 1
    assert len(successes) == 1


@pytest.mark.asyncio
async def test_cancellation_after_store_claim_terminalizes_owned_run() -> None:
    fixture = simple_profit_fixture()
    events = _PausingClaimStore()
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=InlineResultStore(),
        event_store=events,
    )
    task = asyncio.create_task(
        coordinator.run(fixture.plan, run_id="run-claim-cancel")
    )
    await events.claimed.wait()
    task.cancel()
    events.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    recorded = await events.list("run-claim-cancel")
    assert any(event.code == "RUN_CANCELLED" for event in recorded)
    with pytest.raises(RuntimeFailure) as reused:
        await coordinator.run(fixture.plan, run_id="run-claim-cancel")
    assert reused.value.code == "RUN_ID_CONFLICT"


@pytest.mark.asyncio
async def test_cancellation_during_claim_registration_cleans_reservation() -> None:
    fixture = simple_profit_fixture()
    events = _PausingClaimStore("run-register-cancel")
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=InlineResultStore(),
        event_store=events,
    )
    task = asyncio.create_task(
        coordinator.run(fixture.plan, run_id="run-register-cancel")
    )
    await events.claimed.wait()
    await coordinator._run_lock.acquire()
    events.release.set()
    await asyncio.sleep(0)
    task.cancel()
    coordinator._run_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "run-register-cancel" not in coordinator._claiming_run_ids
    recorded = await events.list("run-register-cancel")
    assert any(event.code == "RUN_CANCELLED" for event in recorded)


@pytest.mark.asyncio
async def test_cancelled_store_claim_propagates_without_spinning() -> None:
    fixture = simple_profit_fixture()
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=InlineResultStore(),
        event_store=_CancellingClaimStore(),
    )
    task = asyncio.create_task(
        coordinator.run(fixture.plan, run_id="run-store-cancel")
    )
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.1)


@pytest.mark.asyncio
async def test_slow_claim_does_not_block_unrelated_run_cancellation() -> None:
    events = _PausingClaimStore()
    active_fixture = delayed_fixture()
    waiting_fixture = simple_profit_fixture()
    coordinator = QueryCoordinator(
        resolver=active_fixture.resolver,
        result_store=InlineResultStore(),
        event_store=events,
    )
    active = asyncio.create_task(
        coordinator.run(active_fixture.plan, run_id="active-run")
    )
    await _wait_for_run_started(coordinator, "active-run")
    waiting = asyncio.create_task(
        coordinator.run(waiting_fixture.plan, run_id="run-claim-cancel")
    )
    await events.claimed.wait()
    assert await asyncio.wait_for(coordinator.cancel("active-run"), timeout=0.05)
    events.release.set()
    active_outcome, waiting_outcome = await asyncio.gather(active, waiting)
    assert active_outcome.summary.state is ExecutionState.CANCELLED
    assert waiting_outcome.summary.state is ExecutionState.SUCCEEDED


@pytest.mark.asyncio
async def test_outer_task_cancellation_terminalizes_run(tmp_path: Path) -> None:
    fixture = complex_profit_fixture()
    fixture.resolver._delays["regional_source"] = 1.0
    events = InMemoryEventStore()
    store = ParquetResultStore(tmp_path)
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=store,
        event_store=events,
    )
    task = asyncio.create_task(coordinator.run(fixture.plan, run_id="run-task-cancel"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    recorded = await events.list("run-task-cancel")
    assert any(event.code == "RUN_CANCELLED" for event in recorded)
    terminal_nodes = {
        event.scope_id
        for event in recorded
        if event.scope == "node"
        and event.state
        in {
            ExecutionState.CANCELLED,
            ExecutionState.FAILED,
            ExecutionState.SKIPPED,
            ExecutionState.SUCCEEDED,
            ExecutionState.TIMED_OUT,
        }
    }
    assert terminal_nodes == {node.id for node in fixture.plan.nodes}
    await store.wait_for_cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_code", ("NODE_SUCCEEDED", "NODE_TIMEOUT"))
async def test_outer_cancellation_during_node_terminal_emission_preserves_outcome(
    terminal_code: str, tmp_path: Path
) -> None:
    fixture = (
        simple_profit_fixture()
        if terminal_code == "NODE_SUCCEEDED"
        else delayed_fixture()
    )
    events = _PausingNodeTerminalEventStore(terminal_code)
    limits = ResourceLimits(
        node_timeout_seconds=(
            10.0 if terminal_code == "NODE_SUCCEEDED" else 0.01
        )
    )
    store = ParquetResultStore(tmp_path)
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=store,
        event_store=events,
        limits=limits,
    )
    task = asyncio.create_task(
        coordinator.run(fixture.plan, run_id=f"run-paused-{terminal_code.lower()}")
    )
    await events.node_terminal.wait()
    task.cancel()
    events.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    recorded = await events.list(f"run-paused-{terminal_code.lower()}")
    node_id = fixture.plan.output_node_id
    assert any(
        event.scope_id == node_id and event.code == terminal_code
        for event in recorded
    )
    assert not any(
        event.scope_id == node_id and event.code == "NODE_CANCELLED"
        for event in recorded
    )
    assert any(event.code == "STAGE_CANCELLED" for event in recorded)
    assert any(event.code == "RUN_CANCELLED" for event in recorded)
    await store.wait_for_cleanup()
    await _wait_for_coordinator_cleanup(coordinator)


@pytest.mark.asyncio
async def test_node_timeout_does_not_wait_for_parquet_writer(tmp_path: Path) -> None:
    fixture = simple_profit_fixture()
    store = _SlowParquetStore(tmp_path)
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=store,
        limits=ResourceLimits(node_timeout_seconds=0.02),
    )
    started = asyncio.get_running_loop().time()
    outcome = await coordinator.run(fixture.plan, run_id="run-write-timeout")
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.1
    assert outcome.summary.state is ExecutionState.TIMED_OUT
    assert outcome.manifest is None
    await store.wait_for_cleanup()
    await _wait_for_coordinator_cleanup(coordinator)
    assert not list(tmp_path.rglob("_COMMITTED"))  # noqa: ASYNC240
    assert not list(tmp_path.rglob(".tmp-*"))  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_failed_deferred_cleanup_releases_memory_budget(
    tmp_path: Path,
) -> None:
    fixture = simple_profit_fixture()
    store = _FailingSlowCleanupStore(tmp_path)
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=store,
        limits=ResourceLimits(
            max_bytes=100_000,
            max_in_flight_bytes=100_000,
            node_timeout_seconds=0.1,
        ),
    )
    outcome = await coordinator.run(fixture.plan, run_id="run-cleanup-failure")
    assert outcome.summary.state is ExecutionState.TIMED_OUT
    for _ in range(100):
        if store._cleanup_tasks:
            break
        await asyncio.sleep(0.001)
    assert store._cleanup_tasks
    with pytest.raises(RuntimeFailure) as cleanup:
        await store.wait_for_cleanup()
    assert cleanup.value.code == "RESULT_ORPHAN_CLEANUP_FAILED"
    for _ in range(10):
        if coordinator._memory.current == 0:
            break
        await asyncio.sleep(0)
    assert coordinator._memory.current == 0


@pytest.mark.asyncio
async def test_noncooperative_resolver_cannot_extend_node_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = simple_profit_fixture()
    source_table = fixture.resolver._tables["regional_source"]
    cancel_called = asyncio.Event()

    async def stubborn_execute(fragment, cancel_event):
        deadline = asyncio.get_running_loop().time() + 0.2
        while asyncio.get_running_loop().time() < deadline:
            try:
                await asyncio.sleep(deadline - asyncio.get_running_loop().time())
            except asyncio.CancelledError:
                continue
        return source_table

    async def record_cancel(run_id: str, node_id: str) -> None:
        cancel_called.set()

    monkeypatch.setattr(fixture.resolver, "execute", stubborn_execute)
    monkeypatch.setattr(fixture.resolver, "cancel", record_cancel)
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=ParquetResultStore(tmp_path),
        limits=ResourceLimits(
            max_bytes=100_000,
            max_in_flight_bytes=100_000,
            node_timeout_seconds=0.02,
        ),
    )
    started = asyncio.get_running_loop().time()
    outcome = await coordinator.run(fixture.plan, run_id="run-hard-deadline")
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.1
    assert outcome.summary.state is ExecutionState.TIMED_OUT
    await asyncio.wait_for(cancel_called.wait(), timeout=0.05)
    assert coordinator._memory.current == 100_000
    blocked = await coordinator.run(
        fixture.plan, run_id="run-hard-deadline-blocked"
    )
    assert blocked.summary.diagnostic_code == "LIMIT_IN_FLIGHT_BYTES_EXCEEDED"
    await asyncio.sleep(0.25)
    assert coordinator._memory.current == 0


@pytest.mark.asyncio
async def test_timed_out_operator_retains_input_and_output_memory_until_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, resolver, table_bytes = _memory_chain_fixture()
    release = asyncio.Event()
    operator_started = asyncio.Event()
    resolver_calls = 0
    original_resolver_execute = resolver.execute

    async def counted_resolver_execute(fragment, cancel_event):
        nonlocal resolver_calls
        resolver_calls += 1
        return await original_resolver_execute(fragment, cancel_event)

    async def cancellation_resistant_operator(operation, inputs, cancel_event):
        operator_started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return inputs[0]

    monkeypatch.setattr(resolver, "execute", counted_resolver_execute)
    coordinator = QueryCoordinator(
        resolver=resolver,
        result_store=InlineResultStore(),
        limits=ResourceLimits(
            max_bytes=table_bytes,
            max_in_flight_bytes=table_bytes * 2,
            node_timeout_seconds=0.02,
        ),
    )
    monkeypatch.setattr(
        coordinator.executor, "execute", cancellation_resistant_operator
    )

    outcome = await coordinator.run(plan, run_id="run-retained-input")
    assert outcome.summary.state is ExecutionState.TIMED_OUT
    await operator_started.wait()
    assert coordinator._memory.current == table_bytes * 2

    blocked = await coordinator.run(plan, run_id="run-retained-input-blocked")
    assert blocked.summary.diagnostic_code == "LIMIT_IN_FLIGHT_BYTES_EXCEEDED"
    assert resolver_calls == 1

    release.set()
    for _ in range(200):
        if coordinator._memory.current == 0:
            break
        await asyncio.sleep(0.01)
    assert coordinator._memory.current == 0


@pytest.mark.asyncio
async def test_cancellation_during_multi_input_retention_does_not_leak_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = join_sort_fixture()
    operator_release = asyncio.Event()
    first_retained = asyncio.Event()
    retain_release = asyncio.Event()
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=InlineResultStore(),
        limits=ResourceLimits(
            max_bytes=100_000,
            max_in_flight_bytes=300_000,
            node_timeout_seconds=0.02,
        ),
    )

    async def cancellation_resistant_join(operation, inputs, cancel_event):
        while not operator_release.is_set():
            try:
                await operator_release.wait()
            except asyncio.CancelledError:
                continue
        return inputs[0]

    original_retain = coordinator._memory.retain
    retained_count = 0

    async def pausing_retain(run_id: str, node_id: str) -> None:
        nonlocal retained_count
        await original_retain(run_id, node_id)
        retained_count += 1
        if retained_count == 1:
            first_retained.set()
            await retain_release.wait()

    monkeypatch.setattr(coordinator.executor, "execute", cancellation_resistant_join)
    monkeypatch.setattr(coordinator._memory, "retain", pausing_retain)
    task = asyncio.create_task(
        coordinator.run(fixture.plan, run_id="run-retention-race")
    )
    await first_retained.wait()
    task.cancel()
    retain_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    operator_release.set()
    for _ in range(200):
        if coordinator._memory.current == 0:
            break
        await asyncio.sleep(0.01)
    assert coordinator._memory.current == 0
    assert not coordinator._memory._retainers
    assert not coordinator._memory._release_pending


@pytest.mark.asyncio
async def test_global_memory_budget_counts_parallel_tables(tmp_path: Path) -> None:
    fixture = join_sort_fixture()
    budget = 100_000
    outcome = await QueryCoordinator(
        resolver=fixture.resolver,
        result_store=InlineResultStore(),
        limits=ResourceLimits(
            max_bytes=100_000,
            max_in_flight_bytes=budget,
        ),
    ).run(fixture.plan, run_id="run-memory-budget")
    assert outcome.summary.state is ExecutionState.FAILED
    assert any(
        event.code == "LIMIT_IN_FLIGHT_BYTES_EXCEEDED"
        for event in outcome.events
    )


@pytest.mark.asyncio
async def test_intermediate_tables_release_after_final_consumer() -> None:
    plan, resolver, table_bytes = _memory_chain_fixture()
    outcome = await QueryCoordinator(
        resolver=resolver,
        result_store=InlineResultStore(),
        limits=ResourceLimits(
            max_bytes=10_000,
            max_in_flight_bytes=20_000,
        ),
    ).run(plan, run_id="run-memory-release")
    assert outcome.summary.state is ExecutionState.SUCCEEDED


@pytest.mark.asyncio
async def test_cancelled_writer_holds_global_budget_until_cleanup(
    tmp_path: Path,
) -> None:
    plan, resolver, table_bytes = _memory_chain_fixture()
    store = _SlowParquetStore(tmp_path)
    coordinator = QueryCoordinator(
        resolver=resolver,
        result_store=store,
        limits=ResourceLimits(
            max_bytes=table_bytes,
            max_in_flight_bytes=table_bytes,
            node_timeout_seconds=0.02,
        ),
    )
    timed_out = await coordinator.run(plan, run_id="run-writer-owner")
    assert timed_out.summary.state is ExecutionState.TIMED_OUT
    cleanup_waiter = asyncio.create_task(store.wait_for_cleanup())
    await asyncio.sleep(0.01)
    cleanup_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cleanup_waiter
    blocked = await coordinator.run(plan, run_id="run-writer-blocked")
    assert blocked.summary.state is ExecutionState.FAILED
    assert any(
        event.code == "LIMIT_IN_FLIGHT_BYTES_EXCEEDED"
        for event in blocked.events
    )
    await store.wait_for_cleanup()
    for _ in range(10):
        if coordinator._memory.current == 0:
            break
        await asyncio.sleep(0)
    assert coordinator._memory.current == 0


@pytest.mark.asyncio
async def test_pre_admission_rejects_before_resolver_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = simple_profit_fixture()
    called = False

    async def should_not_execute(fragment, cancel_event):
        nonlocal called
        called = True
        return fixture.resolver._tables["regional_source"]

    monkeypatch.setattr(fixture.resolver, "execute", should_not_execute)
    outcome = await QueryCoordinator(
        resolver=fixture.resolver,
        result_store=InlineResultStore(),
        limits=ResourceLimits(
            max_bytes=100,
            max_in_flight_bytes=50,
        ),
    ).run(fixture.plan, run_id="run-pre-admission")
    assert outcome.summary.state is ExecutionState.FAILED
    assert outcome.summary.diagnostic_code == "LIMIT_IN_FLIGHT_BYTES_EXCEEDED"
    assert not called


@pytest.mark.asyncio
async def test_stage_diagnostic_matches_failure_priority() -> None:
    fixture = join_sort_fixture()
    fixture.resolver._delays["region_source"] = 1.0
    fixture.resolver._failures["score_source"] = "synthetic source failure"
    outcome = await QueryCoordinator(
        resolver=fixture.resolver,
        result_store=InlineResultStore(),
    ).run(fixture.plan, run_id="run-diagnostic-priority")
    assert outcome.summary.state is ExecutionState.FAILED
    assert outcome.summary.diagnostic_code == "RESOLVER_SOURCE_FAILED"


@pytest.mark.asyncio
async def test_bounded_concurrency_and_dependency_ordering(tmp_path: Path) -> None:
    fixture = join_sort_fixture()
    fixture.resolver._delays.update({"region_source": 0.03, "score_source": 0.03})
    outcome = await QueryCoordinator(
        resolver=fixture.resolver,
        result_store=ParquetResultStore(tmp_path),
        max_concurrency=1,
    ).run(fixture.plan, run_id="run-bounded")
    assert outcome.summary.state is ExecutionState.SUCCEEDED
    assert fixture.resolver.max_active == 1
    starts = {
        event.scope_id: event.sequence
        for event in outcome.events
        if event.code == "NODE_STARTED"
    }
    successes = {
        event.scope_id: event.sequence
        for event in outcome.events
        if event.code == "NODE_SUCCEEDED"
    }
    assert starts["local-join"] > successes["source-regions"]
    assert starts["local-join"] > successes["source-scores"]
    assert starts["local-sort"] > successes["local-join"]


def test_invalid_dag_is_rejected() -> None:
    with pytest.raises(PlanFailure) as error:
        validate_physical_plan(invalid_dag_fixture())
    assert error.value.code == "PLAN_MISSING_DEPENDENCY"


def test_unreachable_physical_node_is_rejected() -> None:
    fixture = simple_profit_fixture()
    orphan = fixture.plan.nodes[0].model_copy(
        update={"id": "orphan-source", "logical_node_ids": ("orphan-logical",)}
    )
    plan = fixture.plan.model_copy(
        update={"nodes": (*fixture.plan.nodes, orphan)}
    )
    with pytest.raises(PlanFailure) as error:
        validate_physical_plan(plan)
    assert error.value.code == "PLAN_UNREACHABLE_NODE"


def test_inputless_physical_operator_is_rejected() -> None:
    fixture = complex_profit_fixture()
    project = next(
        node for node in fixture.plan.nodes if node.operation is OperatorKind.PROJECT
    ).model_copy(update={"dependencies": (), "wave": 0})
    plan = fixture.plan.model_copy(
        update={"nodes": (project,), "output_node_id": project.id}
    )
    with pytest.raises(PlanFailure) as error:
        validate_physical_plan(plan)
    assert error.value.code == "PLAN_OPERATOR_INPUT_COUNT"


def test_join_with_one_physical_input_is_rejected() -> None:
    fixture = join_sort_fixture()
    join = next(
        node for node in fixture.plan.nodes if node.operation is OperatorKind.JOIN
    )
    source = next(node for node in fixture.plan.nodes if node.id == join.dependencies[0])
    join = join.model_copy(update={"dependencies": (source.id,)})
    plan = fixture.plan.model_copy(
        update={"nodes": (source, join), "output_node_id": join.id}
    )
    with pytest.raises(PlanFailure) as error:
        validate_physical_plan(plan)
    assert error.value.code == "PLAN_OPERATOR_INPUT_COUNT"


def test_source_fragment_with_physical_input_is_rejected() -> None:
    fixture = simple_profit_fixture()
    source = fixture.plan.nodes[0]
    dependent_source = source.model_copy(
        update={"id": "dependent-source", "dependencies": (source.id,), "wave": 1}
    )
    plan = fixture.plan.model_copy(
        update={"nodes": (source, dependent_source), "output_node_id": dependent_source.id}
    )
    with pytest.raises(PlanFailure) as error:
        validate_physical_plan(plan)
    assert error.value.code == "PLAN_SOURCE_FRAGMENT_INPUT"


@pytest.mark.asyncio
async def test_zero_row_fixture_commits_empty_result(tmp_path: Path) -> None:
    fixture = zero_rows_fixture()
    outcome = await QueryCoordinator(
        resolver=fixture.resolver, result_store=ParquetResultStore(tmp_path)
    ).run(fixture.plan, run_id="run-zero")
    assert outcome.summary.state is ExecutionState.SUCCEEDED
    assert outcome.manifest is not None
    assert outcome.manifest.row_count == 0
