from __future__ import annotations

import asyncio

import pyarrow as pa
import pytest

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
    TypedExpression,
)
from query_runtime.errors import OperatorFailure
from query_runtime.operators import DuckDBOperatorExecutor


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
