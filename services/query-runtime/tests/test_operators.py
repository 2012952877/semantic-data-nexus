from __future__ import annotations

import asyncio
import math

import pyarrow as pa
import pytest

from query_runtime.arrow_memory import retained_table_size
from query_runtime.domain import (
    AggregateFunction,
    AggregateSpec,
    BoundPredicate,
    ExpressionKind,
    JoinKey,
    JoinType,
    NamedExpression,
    OperatorKind,
    OperatorSpec,
    ScalarType,
    SortDirection,
    SortSpec,
    TimeGrain,
    TypedExpression,
)
from query_runtime.errors import OperatorFailure, ResourceLimitFailure
from query_runtime.operators import DuckDBOperatorExecutor, ResourceLimits


@pytest.fixture
def executor() -> DuckDBOperatorExecutor:
    return DuckDBOperatorExecutor()


def binary(
    kind: ExpressionKind,
    left: TypedExpression,
    right: TypedExpression,
    data_type: ScalarType,
) -> TypedExpression:
    return TypedExpression(kind=kind, data_type=data_type, args=(left, right))


@pytest.mark.asyncio
async def test_select_filter_aggregate_sort_and_limit(
    executor: DuckDBOperatorExecutor,
) -> None:
    cancel = asyncio.Event()
    table = pa.table(
        {"team": ["A", "A", "B"], "amount": [2.0, 3.0, 9.0], "active": [True, False, True]}
    )
    filtered = await executor.execute(
        OperatorSpec(
            kind=OperatorKind.FILTER,
            predicate=BoundPredicate(
                expression=binary(
                    ExpressionKind.EQUAL,
                    TypedExpression.col("active", ScalarType.BOOLEAN),
                    TypedExpression.literal(True, ScalarType.BOOLEAN),
                    ScalarType.BOOLEAN,
                )
            ),
        ),
        (table,),
        cancel,
    )
    selected = await executor.execute(
        OperatorSpec(kind=OperatorKind.SELECT, columns=("team", "amount")),
        (filtered,),
        cancel,
    )
    aggregated = await executor.execute(
        OperatorSpec(
            kind=OperatorKind.AGGREGATE,
            group_by=("team",),
            aggregates=(
                AggregateSpec(
                    name="total",
                    function=AggregateFunction.SUM,
                    expression=TypedExpression.col("amount", ScalarType.FLOAT),
                ),
            ),
        ),
        (selected,),
        cancel,
    )
    sorted_table = await executor.execute(
        OperatorSpec(
            kind=OperatorKind.SORT,
            sort=(SortSpec(column="total", direction=SortDirection.DESC),),
        ),
        (aggregated,),
        cancel,
    )
    limited = await executor.execute(
        OperatorSpec(kind=OperatorKind.LIMIT, limit=1), (sorted_table,), cancel
    )
    assert limited.to_pylist() == [{"team": "B", "total": 9.0}]


@pytest.mark.asyncio
async def test_pivot_derive_project_and_zero_division(
    executor: DuckDBOperatorExecutor,
) -> None:
    cancel = asyncio.Event()
    table = pa.table(
        {"region": ["N", "N", "S"], "period": ["Q1", "Q2", "Q1"], "profit": [8.0, 4.0, 0.0]}
    )
    pivoted = await executor.execute(
        OperatorSpec(
            kind=OperatorKind.PIVOT,
            pivot_index=("region",),
            pivot_column="period",
            pivot_value="profit",
            pivot_values=("Q1", "Q2"),
        ),
        (table,),
        cancel,
    )
    ratio = binary(
        ExpressionKind.DIVIDE,
        TypedExpression.col("Q1", ScalarType.FLOAT),
        TypedExpression.col("Q2", ScalarType.FLOAT),
        ScalarType.FLOAT,
    )
    derived = await executor.execute(
        OperatorSpec(
            kind=OperatorKind.DERIVE,
            expressions=(NamedExpression(name="ratio", expression=ratio),),
        ),
        (pivoted,),
        cancel,
    )
    projected = await executor.execute(
        OperatorSpec(
            kind=OperatorKind.PROJECT,
            columns=("region", "ratio"),
            expressions=(
                NamedExpression(
                    name="label",
                    expression=TypedExpression.literal("safe", ScalarType.STRING),
                ),
            ),
        ),
        (derived,),
        cancel,
    )
    assert projected.to_pylist() == [
        {"region": "N", "ratio": 2.0, "label": "safe"},
        {"region": "S", "ratio": None, "label": "safe"},
    ]


@pytest.mark.asyncio
async def test_join_and_null_handling(executor: DuckDBOperatorExecutor) -> None:
    left = pa.table({"left_id": [1, 2], "name": ["one", "two"]})
    right = pa.table({"right_id": [2], "value": [None]}, schema=pa.schema([
        pa.field("right_id", pa.int64()), pa.field("value", pa.float64())
    ]))
    joined = await executor.execute(
        OperatorSpec(
            kind=OperatorKind.JOIN,
            join_type=JoinType.LEFT,
            join_keys=(JoinKey(left="left_id", right="right_id"),),
        ),
        (left, right),
        asyncio.Event(),
    )
    assert joined.num_rows == 2
    assert joined.column("value").null_count == 2


@pytest.mark.asyncio
async def test_expression_safety_missing_and_collision_diagnostics(
    executor: DuckDBOperatorExecutor,
) -> None:
    table = pa.table({"safe": [1], "duplicate": [2]})
    with pytest.raises(OperatorFailure) as missing:
        await executor.execute(
            OperatorSpec(
                kind=OperatorKind.SELECT,
                columns=("safe; DROP TABLE input_0",),
            ),
            (table,),
            asyncio.Event(),
        )
    assert missing.value.code == "COLUMN_MISSING"
    with pytest.raises(OperatorFailure) as collision:
        await executor.execute(
            OperatorSpec(
                kind=OperatorKind.DERIVE,
                expressions=(
                    NamedExpression(
                        name="duplicate",
                        expression=TypedExpression.literal(1, ScalarType.INTEGER),
                    ),
                ),
            ),
            (table,),
            asyncio.Event(),
        )
    assert collision.value.code == "COLUMN_COLLISION"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "inputs"),
    (
        (
            OperatorSpec(
                kind=OperatorKind.DERIVE,
                expressions=(
                    NamedExpression(
                        name="FOO",
                        expression=TypedExpression.literal(1, ScalarType.INTEGER),
                    ),
                ),
            ),
            (pa.table({"foo": [1]}),),
        ),
        (
            OperatorSpec(
                kind=OperatorKind.PROJECT,
                columns=("foo",),
                expressions=(
                    NamedExpression(
                        name="FOO",
                        expression=TypedExpression.literal(1, ScalarType.INTEGER),
                    ),
                ),
            ),
            (pa.table({"foo": [1]}),),
        ),
        (
            OperatorSpec(
                kind=OperatorKind.JOIN,
                join_type=JoinType.INNER,
                join_keys=(JoinKey(left="left_id", right="right_id"),),
            ),
            (
                pa.table({"left_id": [1], "foo": [1]}),
                pa.table({"right_id": [1], "FOO": [2]}),
            ),
        ),
        (
            OperatorSpec(
                kind=OperatorKind.PIVOT,
                pivot_index=("foo",),
                pivot_column="period",
                pivot_value="amount",
                pivot_values=("FOO",),
            ),
            (
                pa.table(
                    {"foo": ["x"], "period": ["FOO"], "amount": [1.0]}
                ),
            ),
        ),
    ),
)
async def test_duckdb_normalized_output_collisions_are_rejected(
    executor: DuckDBOperatorExecutor,
    spec: OperatorSpec,
    inputs: tuple[pa.Table, ...],
) -> None:
    with pytest.raises(OperatorFailure) as error:
        await executor.execute(spec, inputs, asyncio.Event())
    assert error.value.code == "COLUMN_COLLISION"


@pytest.mark.asyncio
async def test_duckdb_normalized_input_collision_is_rejected_for_pass_through(
    executor: DuckDBOperatorExecutor,
) -> None:
    table = pa.Table.from_arrays(
        [pa.array([1]), pa.array([2])], names=["foo", "FOO"]
    )
    with pytest.raises(OperatorFailure) as error:
        await executor.execute(
            OperatorSpec(kind=OperatorKind.LIMIT, limit=1),
            (table,),
            asyncio.Event(),
        )
    assert error.value.code == "COLUMN_COLLISION"


@pytest.mark.asyncio
async def test_duckdb_normalization_preserves_distinct_unicode_identifiers(
    executor: DuckDBOperatorExecutor,
) -> None:
    table = pa.table({"ß": [1], "ss": [2]})
    result = await executor.execute(
        OperatorSpec(kind=OperatorKind.PROJECT, columns=("ß", "ss")),
        (table,),
        asyncio.Event(),
    )
    assert result.column_names == ["ß", "ss"]


def test_one_row_slice_retained_backing_buffer_exceeds_table_limit() -> None:
    full = pa.table({"value": list(range(100_000))})
    sliced = full.slice(0, 1)
    assert sliced.nbytes < full.get_total_buffer_size()
    executor = DuckDBOperatorExecutor(
        ResourceLimits(max_bytes=full.get_total_buffer_size() - 1)
    )
    with pytest.raises(ResourceLimitFailure) as error:
        executor.enforce_limits(sliced)
    assert error.value.code == "LIMIT_BYTES_EXCEEDED"


@pytest.mark.parametrize("length", (0, 1))
def test_buffer_view_accounts_retained_root_allocation(length: int) -> None:
    root = pa.allocate_buffer(1024 * 1024)
    view = root.slice(0, length * 8)
    array = pa.Array.from_buffers(pa.int64(), length, [None, view])
    table = pa.table({"value": array})
    assert table.get_total_buffer_size() == length * 8
    executor = DuckDBOperatorExecutor(ResourceLimits(max_bytes=1024))
    with pytest.raises(ResourceLimitFailure) as error:
        executor.enforce_limits(table)
    assert error.value.details["bytes"] == root.size


def test_nested_dictionary_buffers_are_accounted() -> None:
    dictionary = pa.array(["alpha", "beta"]).dictionary_encode()
    nested = pa.StructArray.from_arrays([dictionary], names=["category"])
    table = pa.table({"record": nested})
    assert retained_table_size(table) >= table.get_total_buffer_size()


def test_list_view_nested_dictionary_buffers_are_accounted() -> None:
    dictionary = pa.array(["alpha", "beta"]).dictionary_encode()
    list_view = pa.ListViewArray.from_arrays(
        pa.array([0], type=pa.int32()),
        pa.array([2], type=pa.int32()),
        dictionary,
    )
    table = pa.table({"categories": list_view})
    assert retained_table_size(table) >= table.get_total_buffer_size()


def test_memory_limit_rejects_non_numeric_configuration() -> None:
    with pytest.raises(ValueError, match="integers"):
        ResourceLimits(
            memory_limit_bytes="256MB'; DROP TABLE input_0; --"  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("max_rows", math.nan),
        ("max_bytes", math.inf),
        ("memory_limit_bytes", -math.inf),
        ("node_timeout_seconds", math.nan),
    ),
)
def test_resource_limits_reject_non_finite_values(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        ResourceLimits(**{field: value})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_local_aggregate_rejects_implicit_time_grain(
    executor: DuckDBOperatorExecutor,
) -> None:
    with pytest.raises(OperatorFailure) as error:
        await executor.execute(
            OperatorSpec(
                kind=OperatorKind.AGGREGATE,
                group_by=("recorded_at",),
                time_grain=TimeGrain.MONTH,
                aggregates=(
                    AggregateSpec(
                        name="total",
                        function=AggregateFunction.SUM,
                        expression=TypedExpression.col("amount", ScalarType.FLOAT),
                    ),
                ),
            ),
            (
                pa.table(
                    {
                        "recorded_at": ["2026-01-01"],
                        "amount": [1.0],
                    }
                ),
            ),
            asyncio.Event(),
        )
    assert error.value.code == "OPERATOR_TIME_GRAIN_UNSUPPORTED"
