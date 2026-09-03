from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from query_runtime.coordinator import QueryCoordinator, validate_physical_plan
from query_runtime.domain import ExecutionState
from query_runtime.errors import PlanFailure
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
