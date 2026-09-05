from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal

import pyarrow as pa
import pytest
from query_runtime.domain import (
    AggregateSpec,
    DateSpec,
    ExplodeSpec,
    ImputeSpec,
    OperatorSpecV1,
    ScalarType,
    SortSpec,
    TypedExpression,
    UnpivotSpec,
    WindowSpec,
)
from query_runtime.errors import OperatorFailure, ResourceLimitFailure
from query_runtime.operators import ResourceLimits

from nexus_plugins.runtime import DuckDBComputePlugin


def spec(kind: str, **kwargs: object) -> OperatorSpecV1:
    return OperatorSpecV1.model_validate({"version": "query-runtime/v1", "kind": kind, **kwargs})


async def execute(operation: OperatorSpecV1, *inputs: pa.Table) -> pa.Table:
    return await DuckDBComputePlugin().execute(operation, tuple(inputs), asyncio.Event())


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("UNION_ALL", [1, 1, None, 1, 2, None]),
        ("UNION_DISTINCT", [1, None, 2]),
        ("INTERSECT", [1, None]),
        ("EXCEPT", []),
    ],
)
async def test_set_semantics_duplicates_null_empty(kind: str, expected: list[int | None]) -> None:
    left = pa.table({"id": pa.array([1, 1, None], pa.int64())})
    right = pa.table({"id": pa.array([1, 2, None], pa.int64())})
    result = await execute(spec(kind), left, right)

    def normalize(items: list[int | None]) -> list[int | None]:
        return sorted(items, key=lambda value: (value is None, value or 0))

    assert normalize(result["id"].to_pylist()) == normalize(expected)
    empty = left.slice(0, 0)
    assert (await execute(spec(kind), empty, empty)).schema == empty.schema
    with pytest.raises(OperatorFailure, match="identical"):
        await execute(spec(kind), left, pa.table({"id": ["1"]}))


async def test_decimal_sum_and_exact_literals() -> None:
    value = Decimal("123456789012.123456789012")
    table = pa.table({"amount": pa.array([value, None, value], pa.decimal128(30, 12))})
    for kind in ("AGGREGATE", "SUMMARIZE"):
        result = await execute(
            spec(
                kind,
                aggregates=[
                    AggregateSpec(
                        name="total",
                        function="sum",
                        expression=TypedExpression.col("amount", ScalarType.DECIMAL),
                    )
                ],
            ),
            table,
        )
        assert result["total"][0].as_py() == value * 2
        assert pa.types.is_decimal(result.schema.field("total").type)
    derived = await execute(
        spec(
            "PROJECT",
            expressions=[
                {
                    "name": "exact",
                    "expression": TypedExpression.literal(
                        "0.123456789012345678",
                        ScalarType.DECIMAL,
                    ),
                }
            ],
        ),
        table.slice(0, 1),
    )
    assert derived["exact"][0].as_py() == Decimal("0.123456789012345678")
    with pytest.raises(OperatorFailure, match="strings"):
        await execute(
            spec(
                "PROJECT",
                expressions=[
                    {
                        "name": "lossy",
                        "expression": TypedExpression.literal(0.1, ScalarType.DECIMAL),
                    }
                ],
            ),
            table,
        )


async def test_distinct_deduplicate_pick_and_seeded_sample_are_deterministic() -> None:
    table = pa.table({"key": ["a", "a", "b", "b"], "value": [2, 1, 4, 3]})
    assert (await execute(spec("DISTINCT", columns=["key"]), table)).num_rows == 2
    for kind in ("DEDUPLICATE", "PICK"):
        operation = spec(kind, partition_by=["key"], sort=[SortSpec(column="value")])
        first = await execute(operation, table)
        second = await execute(operation, table.take(pa.array([3, 2, 1, 0])))
        assert first.equals(second)
        assert first["value"].to_pylist() == [1, 3]
    sampled = spec("SAMPLE", sample_seed=42, limit=2)
    assert (await execute(sampled, table)).equals(await execute(sampled, table))
    assert (await execute(spec("SAMPLE", sample_seed=42, limit=0), table)).num_rows == 0


async def test_window_nulls_duplicates_and_explicit_rows_frame() -> None:
    table = pa.table({"key": ["a", "a", "a"], "order": [1, 1, 2], "n": [1, 2, None]})
    cumulative = await execute(
        spec(
            "WINDOW",
            partition_by=["key"],
            sort=[SortSpec(column="order")],
            window=WindowSpec(function="sum", column="n", output="rolling", preceding=1),
        ),
        table,
    )
    assert cumulative["rolling"].to_pylist() == [1, 3, 2]
    for function, expected in (
        ("row_number", [1, 2, 3]),
        ("rank", [1, 1, 3]),
        ("dense_rank", [1, 1, 2]),
    ):
        result = await execute(
            spec(
                "WINDOW",
                sort=[SortSpec(column="order")],
                window=WindowSpec(function=function, output="position"),
            ),
            table,
        )
        assert result["position"].to_pylist() == expected


async def test_date_and_observed_bucket_resample() -> None:
    table = pa.table(
        {
            "d": pa.array([date(2024, 2, 29), date(2024, 2, 1), None], pa.date32()),
            "n": [1, 2, 3],
        }
    )
    bucket = DateSpec(column="d", output="month", grain="month")
    result = await execute(spec("DATE", date=bucket), table)
    assert result["month"].to_pylist() == [date(2024, 2, 1), date(2024, 2, 1), None]
    result = await execute(
        spec(
            "RESAMPLE",
            date=bucket,
            aggregates=[
                AggregateSpec(
                    name="n_sum",
                    function="sum",
                    expression=TypedExpression.col("n", ScalarType.INTEGER),
                )
            ],
        ),
        table,
    )
    assert sorted(result["n_sum"].to_pylist()) == [3, 3]
    timestamp = pa.table({"t": pa.array([datetime(2024, 2, 29, 23, 59, 59, 123456)])})
    result = await execute(
        spec(
            "DATE",
            date=DateSpec(column="t", output="day", grain="day"),
        ),
        timestamp,
    )
    assert result["t"][0].as_py().microsecond == 123456
    with pytest.raises(OperatorFailure, match="date or timestamp"):
        await execute(spec("DATE", date=bucket), pa.table({"d": ["2024-02-01"]}))


async def test_unpivot_explode_and_impute_preserve_nulls_and_types() -> None:
    table = pa.table({"id": [1, 2], "a": [3, None], "b": [4, 5]})
    result = await execute(
        spec(
            "UNPIVOT",
            unpivot=UnpivotSpec(
                columns=("a", "b"),
                name_column="metric",
                value_column="value",
            ),
        ),
        table,
    )
    assert result.num_rows == 4
    assert result["value"].null_count == 1
    assert result.schema.field("value").type == pa.int64()
    exploded = await execute(
        spec(
            "EXPLODE",
            explode=ExplodeSpec(
                column="items",
                output="item",
            ),
        ),
        pa.table({"items": pa.array([[1, None], [], None], pa.list_(pa.int64()))}),
    )
    assert exploded["item"].to_pylist() == [1, None]
    imputed = await execute(
        spec(
            "IMPUTE",
            impute=ImputeSpec(
                column="a",
                value=TypedExpression.literal(0, ScalarType.INTEGER),
            ),
        ),
        table,
    )
    assert imputed["a"].to_pylist() == [3, 0]
    with pytest.raises(OperatorFailure, match="identical Arrow types"):
        await execute(
            spec(
                "UNPIVOT",
                unpivot=UnpivotSpec(
                    columns=("a", "b"),
                    name_column="metric",
                    value_column="value",
                ),
            ),
            pa.table({"a": [1], "b": ["x"]}),
        )


async def test_bounded_output_memory_no_spool_and_pre_cancel() -> None:
    compute = DuckDBComputePlugin(ResourceLimits(max_rows=2, max_bytes=1024))
    table = pa.table({"id": [1, 2]})
    with pytest.raises(ResourceLimitFailure, match="row limit"):
        await compute.execute(spec("UNION_ALL"), (table, table), asyncio.Event())
    cancelled = asyncio.Event()
    cancelled.set()
    with pytest.raises(asyncio.CancelledError):
        await compute.execute(spec("DISTINCT"), (table,), cancelled)
    compute = DuckDBComputePlugin(ResourceLimits(max_bytes=1))
    with pytest.raises(ResourceLimitFailure):
        await compute.execute(spec("DISTINCT"), (table,), asyncio.Event())
    compute = DuckDBComputePlugin(ResourceLimits(memory_limit_bytes=1024))
    with pytest.raises(OperatorFailure, match="DuckDB"):
        await compute.execute(
            spec("SORT", sort=[SortSpec(column="id")]),
            (pa.table({"id": list(range(10000))}),),
            asyncio.Event(),
        )


async def test_imputation_preserves_declared_decimal_and_narrow_integer_types() -> None:
    for arrow_type, scalar_type, fill in (
        (pa.int16(), ScalarType.INTEGER, 12),
        (pa.decimal128(12, 2), ScalarType.DECIMAL, "12.34"),
        (pa.date32(), ScalarType.DATE, "2024-02-29"),
    ):
        table = pa.table({"value": pa.array([None], type=arrow_type)})
        result = await execute(
            spec(
                "IMPUTE",
                impute=ImputeSpec(
                    column="value",
                    value=TypedExpression.literal(fill, scalar_type),
                ),
            ),
            table,
        )
        assert result.schema == table.schema
        assert result["value"].null_count == 0
    with pytest.raises(OperatorFailure, match="without loss"):
        await execute(
            spec(
                "IMPUTE",
                impute=ImputeSpec(
                    column="value",
                    value=TypedExpression.literal("1.234", ScalarType.DECIMAL),
                ),
            ),
            pa.table({"value": pa.array([None], type=pa.decimal128(12, 2))}),
        )
