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
    assert any(event.code == "RESOLVER_CANCEL_FAILED" for event in outcome.events)
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
        limits=ResourceLimits(node_timeout_seconds=0.02),
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
    assert any(event.code == "RESOLVER_CANCEL_TIMEOUT" for event in outcome.events)
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
    assert not list(tmp_path.rglob("_COMMITTED"))  # noqa: ASYNC240
    assert not list(tmp_path.rglob(".tmp-*"))  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_global_memory_budget_counts_parallel_tables(tmp_path: Path) -> None:
    fixture = join_sort_fixture()
    tables = fixture.resolver._tables
    budget = sum(table.nbytes for table in tables.values()) - 1
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
            max_bytes=100_000,
            max_in_flight_bytes=table_bytes * 3 + 1_000,
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
            max_bytes=100_000,
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


@pytest.mark.asyncio
async def test_zero_row_fixture_commits_empty_result(tmp_path: Path) -> None:
    fixture = zero_rows_fixture()
    outcome = await QueryCoordinator(
        resolver=fixture.resolver, result_store=ParquetResultStore(tmp_path)
    ).run(fixture.plan, run_id="run-zero")
    assert outcome.summary.state is ExecutionState.SUCCEEDED
    assert outcome.manifest is not None
    assert outcome.manifest.row_count == 0
