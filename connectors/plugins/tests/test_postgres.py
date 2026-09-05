"""Real PostgreSQL service tests. Missing service configuration is an error, never a skip."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pyarrow as pa
import pytest
from conftest import SyntheticCredentials, asset, context, fragment
from query_runtime.domain import (
    BoundPredicate,
    ExpressionKind,
    OperatorKind,
    OperatorSpec,
    ScalarType,
    SortSpec,
    TypedExpression,
)
from query_runtime.errors import ResolverFailure, ResourceLimitFailure
from query_runtime.operators import ResourceLimits

from nexus_plugins.contracts import CredentialReference
from nexus_plugins.postgres import PostgreSQLResolver

pytestmark = pytest.mark.postgres

SCHEMA = pa.schema(
    [
        ("id", pa.int64()),
        ("name", pa.string()),
        ("amount", pa.decimal128(24, 12)),
        ("day", pa.date32()),
        ("instant", pa.timestamp("us")),
        ("enabled", pa.bool_()),
    ]
)


@pytest.fixture
def database() -> Iterator[tuple[dict[str, str], psycopg.Connection[tuple[Any, ...]]]]:
    required = ["HOST", "PORT", "DB", "ADMIN", "PASSWORD"]
    if any(f"NEXUS_TEST_PG_{name}" not in os.environ for name in required):
        pytest.fail("Real PostgreSQL configuration is required; select -m 'not postgres' locally")
    settings = {
        "host": os.environ["NEXUS_TEST_PG_HOST"],
        "port": os.environ["NEXUS_TEST_PG_PORT"],
        "dbname": os.environ["NEXUS_TEST_PG_DB"],
        "user": os.environ["NEXUS_TEST_PG_ADMIN"],
        "password": os.environ["NEXUS_TEST_PG_PASSWORD"],
    }
    with psycopg.connect(**settings, autocommit=True) as admin:
        assert admin.info.server_version // 10000 == 16
        admin.execute("DROP SCHEMA IF EXISTS nexus_synthetic CASCADE")
        admin.execute("DROP ROLE IF EXISTS nexus_synthetic_reader")
        admin.execute("CREATE ROLE nexus_synthetic_reader LOGIN PASSWORD 'synthetic-reader-only'")
        admin.execute("CREATE SCHEMA nexus_synthetic")
        admin.execute(
            "CREATE TABLE nexus_synthetic.orders "
            "(id BIGINT, name TEXT, amount NUMERIC(24,12), day DATE, "
            "instant TIMESTAMP, enabled BOOLEAN)"
        )
        admin.execute(
            "INSERT INTO nexus_synthetic.orders VALUES (%s,%s,%s,%s,%s,%s),(%s,%s,%s,%s,%s,%s)",
            (
                1,
                "quoted ' ; synthetic",
                Decimal("1.123456789012"),
                date(2024, 2, 29),
                datetime(2024, 2, 29, 1, 2, 3, 123456),
                True,
                2,
                None,
                None,
                None,
                None,
                None,
            ),
        )
        admin.execute("REVOKE ALL ON SCHEMA nexus_synthetic FROM PUBLIC")
        admin.execute("GRANT USAGE ON SCHEMA nexus_synthetic TO nexus_synthetic_reader")
        admin.execute("GRANT SELECT ON nexus_synthetic.orders TO nexus_synthetic_reader")
        reader = {**settings, "user": "nexus_synthetic_reader", "password": "synthetic-reader-only"}
        try:
            yield reader, admin
        finally:
            admin.execute("DROP SCHEMA nexus_synthetic CASCADE")
            admin.execute("DROP ROLE nexus_synthetic_reader")


def plugin(settings: dict[str, str], *, limits: ResourceLimits | None = None) -> PostgreSQLResolver:
    return PostgreSQLResolver(
        [asset("postgresql", schema=SCHEMA, table=("nexus_synthetic", "orders"))],
        CredentialReference(id="credential:synthetic-postgres"),
        SyntheticCredentials(settings),
        limits=limits,
    )


async def test_real_postgres_discovery_schema_readonly_and_bound_filter(
    database: tuple[dict[str, str], psycopg.Connection[tuple[Any, ...]]],
) -> None:
    settings, _ = database
    resolver = plugin(settings)
    item = resolver.discover()[0]
    assert await resolver.schema("orders") == SCHEMA
    assert await resolver.health()
    selected = await resolver.execute_validated_fragment(
        context(),
        fragment(
            item,
            OperatorSpec(
                kind=OperatorKind.FILTER,
                predicate=BoundPredicate(
                    expression=TypedExpression(
                        kind=ExpressionKind.EQUAL,
                        data_type=ScalarType.BOOLEAN,
                        args=(
                            TypedExpression.col("name", ScalarType.STRING),
                            TypedExpression.literal("quoted ' ; synthetic", ScalarType.STRING),
                        ),
                    ),
                ),
            ),
            OperatorSpec(kind=OperatorKind.SELECT, columns=("amount", "day", "instant", "enabled")),
        ),
        asyncio.Event(),
    )
    assert selected.to_pylist() == [
        {
            "amount": Decimal("1.123456789012"),
            "day": date(2024, 2, 29),
            "instant": datetime(2024, 2, 29, 1, 2, 3, 123456),
            "enabled": True,
        }
    ]
    table = await resolver.execute_validated_fragment(
        context(),
        fragment(
            item,
            OperatorSpec(kind=OperatorKind.SORT, sort=(SortSpec(column="id"),)),
        ),
        asyncio.Event(),
    )
    assert table["amount"].to_pylist() == [Decimal("1.123456789012"), None]
    assert not resolver._connections and not resolver._active


async def test_real_postgres_rejects_writer_and_schema_drift(
    database: tuple[dict[str, str], psycopg.Connection[tuple[Any, ...]]],
) -> None:
    settings, admin = database
    resolver = plugin(settings)
    admin.execute("GRANT UPDATE ON nexus_synthetic.orders TO nexus_synthetic_reader")
    with pytest.raises(ResolverFailure, match="write/DDL"):
        await resolver.connection_test()
    admin.execute("REVOKE UPDATE ON nexus_synthetic.orders FROM nexus_synthetic_reader")
    admin.execute("ALTER TABLE nexus_synthetic.orders ALTER id TYPE TEXT")
    with pytest.raises(ResolverFailure, match="schema drift"):
        await resolver.connection_test()


async def test_real_postgres_resource_limit_and_cancel_lock_wait(
    database: tuple[dict[str, str], psycopg.Connection[tuple[Any, ...]]],
) -> None:
    settings, admin = database
    bounded = plugin(settings, limits=ResourceLimits(max_rows=1))
    with pytest.raises(ResourceLimitFailure):
        await bounded.execute_validated_fragment(
            context(),
            fragment(bounded.discover()[0]),
            asyncio.Event(),
        )
    resolver = plugin(settings)
    with admin.transaction():
        admin.execute("LOCK TABLE nexus_synthetic.orders IN ACCESS EXCLUSIVE MODE")
        work = asyncio.create_task(
            resolver.execute_validated_fragment(
                context(),
                fragment(resolver.discover()[0]),
                asyncio.Event(),
            )
        )
        async with asyncio.timeout(2):
            while True:
                waiting = admin.execute(
                    "SELECT count(*) FROM pg_catalog.pg_stat_activity "
                    "WHERE usename = 'nexus_synthetic_reader' AND wait_event_type = 'Lock'"
                ).fetchone()
                if waiting and waiting[0]:
                    break
                await asyncio.sleep(0.01)
        await resolver.cancel("synthetic-handle")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(work, timeout=5)
    assert not resolver._connections and not resolver._active
    await resolver.connection_test()


def test_postgres_server_itself_rejects_mutation(
    database: tuple[dict[str, str], psycopg.Connection[tuple[Any, ...]]],
) -> None:
    settings, _ = database
    with psycopg.connect(**settings) as connection:
        connection.read_only = True
        assert connection.execute("SHOW transaction_read_only").fetchone() == ("on",)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute("DELETE FROM nexus_synthetic.orders")
        connection.rollback()
        connection.read_only = False
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("DELETE FROM nexus_synthetic.orders")
        connection.rollback()
