from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import asset
from pydantic import ValidationError
from query_runtime.domain import (
    V0_OPERATOR_KINDS,
    AggregateFunction,
    AggregateSpec,
    ExecutionState,
    ExpressionKind,
    ImputeSpec,
    JoinKey,
    JoinType,
    NamedExpression,
    OperatorKind,
    OperatorSpec,
    OperatorSpecV1,
    PhysicalPlan,
    ScalarType,
    SortSpec,
    SourceFragment,
    TypedExpression,
    WindowSpec,
)
from query_runtime.errors import OperatorFailure, PlanFailure
from query_runtime.planner import ExactConceptBinder, LogicalNode, ValidatedLogicalGraph
from query_runtime.resolver import ExecutionContext
from query_runtime.result_store import InlineResultStore, ParquetResultStore

from nexus_plugins.contracts import Asset
from nexus_plugins.files import FileAsset, GovernedFileResolver
from nexus_plugins.runtime import (
    DuckDBComputePlugin,
    PluginRuntime,
    ResolverRegistry,
    ResultStorePlugin,
)


class CountingFileResolver(GovernedFileResolver):
    source_calls = 0

    async def _execute(
        self,
        context: ExecutionContext,
        fragment: SourceFragment,
        asset: Asset,
    ) -> pa.Table:
        self.source_calls += 1
        return await super()._execute(context, fragment, asset)


def runtime_for(
    root: Path,
    table: pa.Table,
    *,
    parquet_results: bool = False,
) -> tuple[PluginRuntime, CountingFileResolver]:
    pq.write_table(table, root / "orders.parquet")
    source = asset(schema=table.schema)
    resolver = CountingFileResolver(root, [FileAsset(source, "orders.parquet")])
    results = ResultStorePlugin(
        ParquetResultStore(root / "results") if parquet_results else InlineResultStore()
    )
    return PluginRuntime(ResolverRegistry([resolver]), DuckDBComputePlugin(), results), resolver


def graph_for(operation: OperatorSpecV1) -> ValidatedLogicalGraph:
    return ValidatedLogicalGraph(
        version="query-runtime/v1",
        id="review-regression",
        output_node_id="operation",
        nodes=(
            LogicalNode(
                id="source",
                source_alias="orders",
                operation=OperatorSpec(kind=OperatorKind.SOURCE),
            ),
            LogicalNode(id="operation", operation=operation, dependencies=("source",)),
        ),
    )


async def run_result(runtime: PluginRuntime, operation: OperatorSpecV1) -> pa.Table:
    plan = await runtime.registry.plan(graph_for(operation), ExactConceptBinder({}))
    outcome = await runtime.run(plan)
    assert outcome.summary.state is ExecutionState.SUCCEEDED, outcome.events
    assert outcome.manifest is not None
    return await runtime.results.read_page(outcome.manifest.result, 0, 100)


def coalesce(*arguments: TypedExpression) -> TypedExpression:
    return TypedExpression(
        kind=ExpressionKind.COALESCE,
        data_type=ScalarType.DECIMAL,
        args=arguments,
    )


@pytest.mark.parametrize("fallback", ["0", None])
@pytest.mark.parametrize(
    ("precision", "scale", "value"),
    [
        (24, 12, "1.234567890123"),
        (38, 38, "0.12345678901234567890123456789012345678"),
        (38, 12, "12345678901234567890123456.123456789012"),
    ],
)
async def test_decimal_coalesce_full_dispatch_preserves_scale(
    tmp_path: Path,
    precision: int,
    scale: int,
    value: str,
    fallback: str | None,
) -> None:
    decimal = Decimal(value)
    table = pa.table({"amount": pa.array([decimal, None], pa.decimal128(precision, scale))})
    runtime, resolver = runtime_for(tmp_path, table, parquet_results=True)
    result = await run_result(
        runtime,
        OperatorSpecV1(
            version="query-runtime/v1",
            kind=OperatorKind.PROJECT,
            expressions=(
                NamedExpression(
                    name="filled",
                    expression=coalesce(
                        TypedExpression.col("amount", ScalarType.DECIMAL),
                        TypedExpression.literal(fallback, ScalarType.DECIMAL),
                    ),
                ),
            ),
        ),
    )
    assert result["filled"].to_pylist() == [decimal, Decimal("0") if fallback else None]
    assert result.schema.field("filled").type == pa.decimal128(precision, scale)
    assert resolver.source_calls == 1


async def test_decimal_coalesce_infers_wider_scale_and_nested_nulls(tmp_path: Path) -> None:
    table = pa.table({"amount": pa.array([Decimal("1.234567890123"), None], pa.decimal128(24, 12))})
    runtime, _ = runtime_for(tmp_path, table)
    null = TypedExpression.literal(None, ScalarType.DECIMAL)
    expression = coalesce(
        TypedExpression.col("amount", ScalarType.DECIMAL),
        coalesce(null, null),
        TypedExpression.literal("0.000000000000000001", ScalarType.DECIMAL),
    )
    result = await run_result(
        runtime,
        OperatorSpecV1(
            version="query-runtime/v1",
            kind=OperatorKind.DERIVE,
            expressions=(NamedExpression(name="filled", expression=expression),),
        ),
    )
    assert result["filled"].to_pylist() == [
        Decimal("1.234567890123"),
        Decimal("0.000000000000000001"),
    ]
    assert result.schema.field("filled").type == pa.decimal128(30, 18)
    literal = await run_result(
        runtime,
        OperatorSpecV1(
            version="query-runtime/v1",
            kind=OperatorKind.PROJECT,
            expressions=(
                NamedExpression(
                    name="small", expression=TypedExpression.literal("0.0001", ScalarType.DECIMAL)
                ),
                NamedExpression(name="empty", expression=coalesce(null, null)),
            ),
        ),
    )
    assert literal.schema.field("small").type == pa.decimal128(4, 4)
    assert literal["empty"].null_count == 2
    assert pa.types.is_decimal(literal.schema.field("empty").type)


@pytest.mark.parametrize("kind", [OperatorKind.AGGREGATE, OperatorKind.SUMMARIZE])
async def test_decimal_coalesce_aggregate_dispatch(tmp_path: Path, kind: OperatorKind) -> None:
    table = pa.table({"amount": pa.array([Decimal("1.234567890123"), None], pa.decimal128(24, 12))})
    runtime, _ = runtime_for(tmp_path, table)
    result = await run_result(
        runtime,
        OperatorSpecV1(
            version="query-runtime/v1",
            kind=kind,
            aggregates=(
                AggregateSpec(
                    name="total",
                    function=AggregateFunction.SUM,
                    expression=coalesce(
                        TypedExpression.col("amount", ScalarType.DECIMAL),
                        TypedExpression.literal("0", ScalarType.DECIMAL),
                    ),
                ),
            ),
        ),
    )
    assert result["total"][0].as_py() == Decimal("1.234567890123")
    assert result.schema.field("total").type.scale == 12


async def test_decimal_unrepresentable_common_type_never_commits_success(tmp_path: Path) -> None:
    table = pa.table({"amount": pa.array([Decimal("0.1")], pa.decimal128(38, 20))})
    runtime, _ = runtime_for(tmp_path, table)
    operation = OperatorSpecV1(
        version="query-runtime/v1",
        kind=OperatorKind.PROJECT,
        expressions=(
            NamedExpression(
                name="filled",
                expression=coalesce(
                    TypedExpression.col("amount", ScalarType.DECIMAL),
                    TypedExpression.literal("1234567890123456789", ScalarType.DECIMAL),
                ),
            ),
        ),
    )
    outcome = await runtime.run(
        await runtime.registry.plan(graph_for(operation), ExactConceptBinder({}))
    )
    assert outcome.summary.state is ExecutionState.FAILED
    assert outcome.manifest is None
    assert any(event.code == "EXPRESSION_TYPE" for event in outcome.events)


MISSING_FORMS: list[dict[str, object]] = [
    {"kind": kind}
    for kind in (
        "FILTER",
        "LIMIT",
        "SELECT",
        "PROJECT",
        "DERIVE",
        "AGGREGATE",
        "SUMMARIZE",
        "SORT",
        "JOIN",
        "PIVOT",
        "DATE",
        "RESAMPLE",
        "WINDOW",
        "UNPIVOT",
        "EXPLODE",
        "IMPUTE",
        "SAMPLE",
    )
] + [
    {"kind": "JOIN", "join_type": JoinType.INNER},
    {"kind": "JOIN", "join_keys": (JoinKey(left="id", right="other_id"),)},
    {"kind": "PIVOT", "pivot_column": "key", "pivot_value": "value"},
    {"kind": "PIVOT", "pivot_column": "key", "pivot_values": ("one",)},
    {
        "kind": "AGGREGATE",
        "aggregates": (AggregateSpec(name="sum", function=AggregateFunction.SUM),),
    },
]


async def assert_preflight_denial(
    tmp_path: Path,
    invalid: OperatorSpecV1,
) -> None:
    runtime, resolver = runtime_for(tmp_path, pa.table({"id": [1], "x": [None]}))
    graph = graph_for(OperatorSpecV1(version="query-runtime/v1", kind=OperatorKind.DISTINCT))
    plan = await runtime.registry.plan(graph, ExactConceptBinder({}))
    bad_graph = graph.model_copy(
        update={
            "nodes": (
                graph.nodes[0],
                graph.nodes[1].model_copy(update={"operation": invalid}),
            )
        }
    )
    with pytest.raises(ValidationError):
        ValidatedLogicalGraph.model_validate(bad_graph.model_dump())
    with pytest.raises(PlanFailure, match="Invalid v1"):
        await runtime.registry.plan(bad_graph, ExactConceptBinder({}))
    bad_plan = plan.model_copy(
        update={
            "nodes": (
                plan.nodes[0],
                plan.nodes[1].model_copy(
                    update={
                        "operation": invalid.kind,
                        "operator": invalid,
                    }
                ),
            )
        }
    )
    with pytest.raises(ValidationError):
        PhysicalPlan.model_validate(bad_plan.model_dump())
    with pytest.raises(PlanFailure, match="Invalid v1"):
        await runtime.run(bad_plan)
    assert resolver.source_calls == 0


@pytest.mark.parametrize("payload", MISSING_FORMS)
async def test_v1_required_forms_are_rejected_before_source_io(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        OperatorSpecV1.model_validate({"version": "query-runtime/v1", **payload})
    kind = OperatorKind(payload["kind"])
    if kind in V0_OPERATOR_KINDS:
        legacy = OperatorSpec.model_validate(payload)
        assert OperatorSpec.model_validate_json(legacy.model_dump_json()) == legacy
    invalid = OperatorSpecV1.model_construct(
        **{
            "version": "query-runtime/v1",
            **payload,
            "kind": kind,
        }
    )
    await assert_preflight_denial(tmp_path, invalid)


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (ScalarType.INTEGER, 1.9),
        (ScalarType.INTEGER, True),
        (ScalarType.INTEGER, "7"),
        (ScalarType.INTEGER, 2**63),
        (ScalarType.BOOLEAN, 1),
        (ScalarType.BOOLEAN, "true"),
        (ScalarType.FLOAT, True),
        (ScalarType.FLOAT, "1.9"),
        (ScalarType.FLOAT, float("inf")),
        (ScalarType.FLOAT, float("nan")),
        (ScalarType.STRING, 7),
        (ScalarType.DECIMAL, 1.9),
        (ScalarType.DECIMAL, "not-decimal"),
        (ScalarType.DECIMAL, "Infinity"),
        (ScalarType.DECIMAL, "1e39"),
        (ScalarType.DATE, "2024-02-30"),
        (ScalarType.DATE, 20240229),
        (ScalarType.TIMESTAMP, "not-a-timestamp"),
        (ScalarType.TIMESTAMP, "2024-02-29T01:02:03+01:00"),
    ],
)
async def test_invalid_imputation_literals_rejected_before_source_io(
    tmp_path: Path,
    kind: ScalarType,
    value: str | int | float | bool,
) -> None:
    expression = TypedExpression.literal(value, kind)
    with pytest.raises(ValidationError):
        ImputeSpec(column="x", value=expression)
    invalid = OperatorSpecV1.model_construct(
        version="query-runtime/v1",
        kind=OperatorKind.IMPUTE,
        impute=ImputeSpec.model_construct(column="x", value=expression),
    )
    await assert_preflight_denial(tmp_path, invalid)
    with pytest.raises(OperatorFailure, match="Invalid v1"):
        await DuckDBComputePlugin().execute(invalid, (pa.table({"x": [None]}),), asyncio.Event())


@pytest.mark.parametrize("parquet_results", [False, True])
async def test_imputation_restores_nonnullable_sibling_and_metadata(
    tmp_path: Path,
    parquet_results: bool,
) -> None:
    schema = pa.schema(
        [
            pa.field("id", pa.int64(), nullable=False, metadata={b"origin": b"synthetic"}),
            pa.field("x", pa.int16()),
        ],
        metadata={b"schema": b"synthetic"},
    )
    table = pa.Table.from_arrays([pa.array([1]), pa.array([None], pa.int16())], schema=schema)
    runtime, _ = runtime_for(tmp_path, table, parquet_results=parquet_results)
    operation = OperatorSpecV1(
        version="query-runtime/v1",
        kind=OperatorKind.IMPUTE,
        impute=ImputeSpec(column="x", value=TypedExpression.literal(7, ScalarType.INTEGER)),
    )
    direct = await DuckDBComputePlugin().execute(operation, (table,), asyncio.Event())
    assert direct.schema.equals(schema, check_metadata=True)
    result = await run_result(runtime, operation)
    assert result.to_pylist() == [{"id": 1, "x": 7}]
    # The file adapter's projection drops table metadata, but retains field metadata.
    assert result.schema.equals(schema.remove_metadata(), check_metadata=True)


async def test_imputation_does_not_hide_nonnullable_output_violations() -> None:
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False), pa.field("x", pa.int64())])
    table = pa.Table.from_arrays(
        [pa.array([None], pa.int64()), pa.array([None], pa.int64())], schema=schema
    )
    operation = OperatorSpecV1(
        version="query-runtime/v1",
        kind=OperatorKind.IMPUTE,
        impute=ImputeSpec(column="x", value=TypedExpression.literal(7, ScalarType.INTEGER)),
    )
    with pytest.raises(OperatorFailure, match="non-nullable"):
        await DuckDBComputePlugin().execute(operation, (table,), asyncio.Event())


@pytest.mark.parametrize(
    "kind",
    [
        OperatorKind.DEDUPLICATE,
        OperatorKind.PICK,
        OperatorKind.SAMPLE,
        OperatorKind.WINDOW,
    ],
)
async def test_explicit_row_identity_handles_shadowing_and_quoted_columns(
    tmp_path: Path,
    kind: OperatorKind,
) -> None:
    table = pa.table(
        {
            "input_0": [0, 0],
            "k": [1, 1],
            "v": ["a", "b"],
            "select": ["reserved", "reserved"],
            'quote"name': ["quoted", "quoted"],
        }
    )
    fields: dict[str, object] = {"sort": (SortSpec(column="k"),)}
    if kind is OperatorKind.SAMPLE:
        fields = {"sample_seed": 42, "limit": 1}
    if kind is OperatorKind.WINDOW:
        fields["window"] = WindowSpec(function="row_number", output="row_number")
    operation = OperatorSpecV1.model_validate(
        {
            "version": "query-runtime/v1",
            "kind": kind,
            **fields,
        }
    )
    runtime, _ = runtime_for(tmp_path, table)
    forward = await run_result(runtime, operation)
    runtime, _ = runtime_for(tmp_path, table.take(pa.array([1, 0])))
    reverse = await run_result(runtime, operation)
    assert forward.equals(reverse)
    if kind in {OperatorKind.DEDUPLICATE, OperatorKind.PICK}:
        assert forward["v"].to_pylist() == ["a"]


def test_plugin_conformance_is_mandatory_in_the_unfiltered_m0_gate() -> None:
    workflows = Path(__file__).resolve().parents[3] / ".github" / "workflows"
    plugin = (workflows / "plugin-conformance.yml").read_text()
    aggregate = (workflows / "m0-release-gate.yml").read_text()
    assert "  workflow_call:" in plugin
    assert "paths:" not in aggregate
    assert "  plugin_conformance:" in aggregate
    assert "uses: ./.github/workflows/plugin-conformance.yml" in aggregate
    release_gate = aggregate.split("  release_gate:", 1)[1]
    assert "if: always()" in release_gate
    assert "      - plugin_conformance" in release_gate
    assert "PLUGIN_CONFORMANCE: ${{ needs.plugin_conformance.result }}" in release_gate
    assert '"plugin_conformance=$PLUGIN_CONFORMANCE"' in release_gate
    assert '[ "$result" != "success" ]' in release_gate
