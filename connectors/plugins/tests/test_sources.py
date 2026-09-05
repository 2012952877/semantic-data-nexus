from __future__ import annotations

import asyncio
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as csv
import pyarrow.parquet as pq
import pytest
from conftest import asset, context, fragment
from query_runtime.domain import (
    BoundPredicate,
    ExpressionKind,
    OperatorKind,
    OperatorSpec,
    ScalarType,
    TypedExpression,
)
from query_runtime.errors import ResolverFailure, ResourceLimitFailure, RuntimeFailure
from query_runtime.operators import ResourceLimits
from sqlglot import exp, parse_one

from nexus_plugins.compiler import compile_read
from nexus_plugins.files import FileAsset, GovernedFileResolver


def predicate(value: str = "quoted ' ; synthetic") -> BoundPredicate:
    return BoundPredicate(
        expression=TypedExpression(
            kind=ExpressionKind.EQUAL,
            data_type=ScalarType.BOOLEAN,
            args=(
                TypedExpression.col("name", ScalarType.STRING),
                TypedExpression.literal(value, ScalarType.STRING),
            ),
        )
    )


@pytest.mark.parametrize("dialect", ["postgres", "databricks"])
def test_parameter_binding_and_asset_identifiers(dialect: str) -> None:
    item = asset(
        table=("synthetic", "orders")
        if dialect == "postgres"
        else ("synthetic", "public", "orders")
    )
    compiled = compile_read(
        fragment(
            item,
            OperatorSpec(kind=OperatorKind.FILTER, predicate=predicate()),
            OperatorSpec(kind=OperatorKind.SELECT, columns=("id",)),
        ),
        item,
        dialect,
        ResourceLimits(),
    )
    assert "quoted" not in compiled.sql
    assert compiled.values[0] == "quoted ' ; synthetic"
    assert compiled.schema.names == ["id"]
    assert "%(p0)s" in compiled.sql if dialect == "postgres" else ":p0" in compiled.sql
    tree = parse_one(compiled.sql, read=dialect)
    outer_limit = tree.args["limit"].expression
    assert compiled.values[int(outer_limit.name[1:])] == ResourceLimits().max_rows + 1
    where = next(tree.find_all(exp.Where))
    marker = next(where.find_all(exp.Placeholder))
    assert compiled.values[int(marker.name[1:])] == "quoted ' ; synthetic"
    for operation in (
        OperatorSpec(kind=OperatorKind.JOIN),
        OperatorSpec(kind=OperatorKind.SELECT, columns=("id",), limit=1),
        OperatorSpec(kind=OperatorKind.SELECT, columns=("unknown",)),
    ):
        with pytest.raises(RuntimeFailure):
            compile_read(fragment(item, operation), item, dialect, ResourceLimits())


@pytest.mark.parametrize("extension", ["csv", "parquet"])
async def test_real_file_projection_filter_precision_nulls_and_schema(
    tmp_path: Path,
    extension: str,
) -> None:
    table = pa.table(
        {
            "id": [1, 2, 3],
            "name": ["quoted ' ; synthetic", "other", None],
            "amount": pa.array(
                [Decimal("1.123456789012"), None, Decimal("2.000000000001")], pa.decimal128(24, 12)
            ),
            "day": pa.array([date(2024, 2, 29), None, date(2024, 3, 1)], pa.date32()),
        }
    )
    path = tmp_path / f"orders.{extension}"
    (csv.write_csv if extension == "csv" else pq.write_table)(table, path)
    item = asset(schema=table.schema)
    resolver = GovernedFileResolver(tmp_path, [FileAsset(item, path.name)])
    assert resolver.discover() == (item,)
    assert await resolver.schema("orders") == table.schema
    await resolver.connection_test()
    result = await resolver.execute_validated_fragment(
        context(),
        fragment(
            item,
            OperatorSpec(kind=OperatorKind.FILTER, predicate=predicate()),
            OperatorSpec(kind=OperatorKind.SELECT, columns=("amount", "day")),
        ),
        asyncio.Event(),
    )
    assert result.to_pylist() == [{"amount": Decimal("1.123456789012"), "day": date(2024, 2, 29)}]
    all_rows = await resolver.execute_validated_fragment(context(), fragment(item), asyncio.Event())
    assert all_rows.equals(table)
    with pytest.raises(ResolverFailure, match="inventory"):
        await resolver.execute_validated_fragment(
            context(),
            fragment(asset(alias="unlisted")),
            asyncio.Event(),
        )


@pytest.mark.parametrize(
    "relative",
    [
        "../orders.csv",
        "/orders.csv",
        "C:\\orders.csv",
        "orders.txt",
        "orders.csv.gz",
        "orders.csv:stream",
        "folder\\orders.csv",
    ],
)
async def test_file_path_boundaries(tmp_path: Path, relative: str) -> None:
    item = asset()
    resolver = GovernedFileResolver(tmp_path, [FileAsset(item, relative)])
    with pytest.raises(ResolverFailure, match="paths"):
        await resolver.execute_validated_fragment(context(), fragment(item), asyncio.Event())


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows symlink privilege unavailable; Linux CI"
)
async def test_file_symlink_denial(tmp_path: Path) -> None:
    item = asset()
    target = tmp_path / "real.csv"
    csv.write_csv(pa.table({"id": [1], "name": ["synthetic"]}), target)
    link = tmp_path / "link.csv"
    link.symlink_to(target)
    resolver = GovernedFileResolver(tmp_path, [FileAsset(item, link.name)])
    with pytest.raises(ResolverFailure, match="Symlinks"):
        await resolver.execute_validated_fragment(context(), fragment(item), asyncio.Event())


async def test_file_size_denial(tmp_path: Path) -> None:
    item = asset()
    target = tmp_path / "real.csv"
    csv.write_csv(pa.table({"id": [1], "name": ["synthetic"]}), target)
    resolver = GovernedFileResolver(tmp_path, [FileAsset(item, target.name)], max_file_bytes=1)
    with pytest.raises(ResourceLimitFailure):
        await resolver.execute_validated_fragment(context(), fragment(item), asyncio.Event())


async def test_file_limits_cancellation_and_schema_drift(tmp_path: Path) -> None:
    item = asset()
    path = tmp_path / "orders.parquet"
    pq.write_table(pa.table({"id": [1, 2, 3], "name": ["a", None, "b"]}), path)
    resolver = GovernedFileResolver(
        tmp_path,
        [FileAsset(item, path.name)],
        limits=ResourceLimits(max_rows=2),
    )
    with pytest.raises(ResourceLimitFailure):
        await resolver.execute_validated_fragment(context(), fragment(item), asyncio.Event())
    event = asyncio.Event()
    event.set()
    with pytest.raises(asyncio.CancelledError):
        await resolver.execute_validated_fragment(context(), fragment(item), event)
    assert not resolver._active
    with pytest.raises(ResolverFailure, match="not active"):
        await resolver.cancel("unknown")
    pq.write_table(pa.table({"id": ["changed"], "name": ["synthetic"]}), path)
    with pytest.raises(ResolverFailure, match="drift"):
        await resolver.schema("orders")
