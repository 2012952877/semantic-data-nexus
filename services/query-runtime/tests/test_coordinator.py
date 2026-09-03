from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from query_runtime.coordinator import QueryCoordinator, validate_physical_plan
from query_runtime.domain import DiagnosticEvent, ExecutionState
from query_runtime.errors import PlanFailure
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
from query_runtime.result_store import ParquetResultStore


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
    await asyncio.sleep(0.005)
    started = asyncio.get_running_loop().time()
    assert await coordinator.cancel("run-stubborn-cancel")
    outcome = await task
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.1
    assert outcome.summary.state is ExecutionState.CANCELLED
    assert any(event.code == "RESOLVER_CANCEL_TIMEOUT" for event in outcome.events)
    await asyncio.sleep(0.25)


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
