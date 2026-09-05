from __future__ import annotations

import asyncio
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import asset
from query_runtime.domain import (
    BoundPredicate,
    ExecutionState,
    ExpressionKind,
    OperatorKind,
    OperatorSpec,
    OperatorSpecV1,
    ScalarType,
    TypedExpression,
)
from query_runtime.errors import PlanFailure, ResolverFailure
from query_runtime.planner import ExactConceptBinder, LogicalNode, ValidatedLogicalGraph
from query_runtime.result_store import InlineResultStore, ParquetResultStore

from nexus_plugins.files import FileAsset, GovernedFileResolver
from nexus_plugins.runtime import (
    DuckDBComputePlugin,
    PluginRuntime,
    ResolverRegistry,
    ResultStorePlugin,
)


@pytest.mark.parametrize("store_kind", ["inline", "parquet"])
async def test_planner_registry_compute_coordinator_result_store(
    tmp_path: Path,
    store_kind: str,
) -> None:
    table = pa.table({"id": [1, 1, 2], "name": ["a", "a", "b"]})
    pq.write_table(table, tmp_path / "orders.parquet")
    item = asset(schema=table.schema)
    resolver = GovernedFileResolver(tmp_path, [FileAsset(item, "orders.parquet")])
    registry = ResolverRegistry([resolver])
    graph = ValidatedLogicalGraph(
        version="query-runtime/v1",
        id="dispatch",
        output_node_id="distinct",
        nodes=(
            LogicalNode(
                id="source",
                source_alias="orders",
                operation=OperatorSpec(kind=OperatorKind.SOURCE),
            ),
            LogicalNode(
                id="distinct",
                dependencies=("source",),
                operation=OperatorSpecV1(version="query-runtime/v1", kind=OperatorKind.DISTINCT),
            ),
        ),
    )
    plan = await registry.plan(graph, ExactConceptBinder({}))
    results = ResultStorePlugin(
        InlineResultStore() if store_kind == "inline" else ParquetResultStore(tmp_path / "results")
    )
    runtime = PluginRuntime(registry, DuckDBComputePlugin(), results)
    outcome = await runtime.run(plan)
    assert outcome.summary.state is ExecutionState.SUCCEEDED
    assert outcome.manifest is not None
    assert outcome.manifest.row_count == 2
    result = await results.read_page(outcome.manifest.result, 0, 10)
    assert result.to_pylist() == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    completed = {event.scope_id for event in outcome.events if event.code == "NODE_SUCCEEDED"}
    assert completed == {"physical-source", "physical-distinct"}
    assert not registry._active
    with pytest.raises(ResolverFailure):
        await results.read_page(outcome.manifest.result, 0, results.limits.max_rows + 1)


async def test_two_source_set_dispatch_through_unchanged_lifecycle(tmp_path: Path) -> None:
    table = pa.table({"id": [1, 1], "name": ["a", "a"]})
    pq.write_table(table, tmp_path / "orders.parquet")
    items = [asset(schema=table.schema, alias=alias) for alias in ("left", "right")]
    registry = ResolverRegistry(
        [
            GovernedFileResolver(
                tmp_path,
                [FileAsset(item, "orders.parquet") for item in items],
            )
        ]
    )
    graph = ValidatedLogicalGraph(
        version="query-runtime/v1",
        id="set-dispatch",
        output_node_id="union",
        nodes=(
            *(
                LogicalNode(
                    id=item.source.alias,
                    source_alias=item.source.alias,
                    operation=OperatorSpec(kind=OperatorKind.SOURCE),
                )
                for item in items
            ),
            LogicalNode(
                id="union",
                dependencies=("left", "right"),
                operation=OperatorSpecV1(
                    version="query-runtime/v1",
                    kind=OperatorKind.UNION_ALL,
                ),
            ),
        ),
    )
    plan = await registry.plan(graph, ExactConceptBinder({}))
    runtime = PluginRuntime(registry, DuckDBComputePlugin(), ResultStorePlugin(InlineResultStore()))
    outcome = await runtime.run(plan)
    assert outcome.summary.state is ExecutionState.SUCCEEDED
    assert outcome.manifest.row_count == 4


@pytest.mark.parametrize("kind", [OperatorKind.ASK, OperatorKind.ACT, OperatorKind.SEARCH])
async def test_blocked_physical_plan_is_preflight_not_partial_execution(
    tmp_path: Path,
    kind: OperatorKind,
) -> None:
    item = asset()
    registry = ResolverRegistry([GovernedFileResolver(tmp_path, [FileAsset(item, "missing.csv")])])
    graph = ValidatedLogicalGraph(
        version="query-runtime/v1",
        id="blocked",
        output_node_id="result",
        nodes=(
            LogicalNode(
                id="source", source_alias="orders", operation=OperatorSpec(kind=OperatorKind.SOURCE)
            ),
            LogicalNode(
                id="result",
                dependencies=("source",),
                operation=OperatorSpecV1(
                    version="query-runtime/v1",
                    kind=OperatorKind.DISTINCT,
                ),
            ),
        ),
    )
    plan = await registry.plan(graph, ExactConceptBinder({}))
    node = plan.nodes[-1]
    plan = plan.model_copy(
        update={
            "nodes": (
                plan.nodes[0],
                node.model_copy(
                    update={
                        "operation": kind,
                        "operator": OperatorSpecV1(version="query-runtime/v1", kind=kind),
                    }
                ),
            )
        }
    )
    runtime = PluginRuntime(registry, DuckDBComputePlugin(), ResultStorePlugin(InlineResultStore()))
    with pytest.raises(PlanFailure, match="governed"):
        await runtime.run(plan)
    assert not registry._active


async def test_result_store_pre_cancel_does_not_publish() -> None:
    results = ResultStorePlugin(InlineResultStore())
    cancelled = asyncio.Event()
    cancelled.set()
    with pytest.raises(asyncio.CancelledError):
        await results.commit("synthetic-run", "node", pa.table({"id": [1]}), cancelled)


async def test_supported_arithmetic_predicate_falls_back_to_local_compute(tmp_path: Path) -> None:
    table = pa.table({"id": [1, 2], "name": ["a", "b"]})
    pq.write_table(table, tmp_path / "orders.parquet")
    item = asset(schema=table.schema)
    registry = ResolverRegistry(
        [GovernedFileResolver(tmp_path, [FileAsset(item, "orders.parquet")])]
    )
    add = TypedExpression(
        kind=ExpressionKind.ADD,
        data_type=ScalarType.INTEGER,
        args=(
            TypedExpression.col("id", ScalarType.INTEGER),
            TypedExpression.literal(1, ScalarType.INTEGER),
        ),
    )
    predicate = BoundPredicate(
        expression=TypedExpression(
            kind=ExpressionKind.EQUAL,
            data_type=ScalarType.BOOLEAN,
            args=(add, TypedExpression.literal(2, ScalarType.INTEGER)),
        )
    )
    graph = ValidatedLogicalGraph(
        id="arithmetic",
        output_node_id="filter",
        nodes=(
            LogicalNode(
                id="source", source_alias="orders", operation=OperatorSpec(kind=OperatorKind.SOURCE)
            ),
            LogicalNode(
                id="filter",
                source_alias="orders",
                dependencies=("source",),
                operation=OperatorSpec(kind=OperatorKind.FILTER, predicate=predicate),
            ),
        ),
    )
    plan = await registry.plan(graph, ExactConceptBinder({}))
    assert len(plan.nodes) == 2 and plan.nodes[-1].operator is not None
    results = ResultStorePlugin(InlineResultStore())
    outcome = await PluginRuntime(registry, DuckDBComputePlugin(), results).run(plan)
    assert outcome.summary.state is ExecutionState.SUCCEEDED
    result = await results.read_page(outcome.manifest.result, 0, 10)
    assert result["id"].to_pylist() == [1]
