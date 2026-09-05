from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pyarrow as pa
import pytest
from conftest import SyntheticCredentials, asset, context, fragment, statement_response
from query_runtime.domain import OperatorKind, OperatorSpec
from query_runtime.errors import ResolverFailure
from semantic_data_nexus_databricks.config import ResolverConfig
from semantic_data_nexus_databricks.testing import FakeTransport

from nexus_plugins.contracts import CredentialReference
from nexus_plugins.databricks import DatabricksPlugin

BASE = "https://workspace.example.invalid"


async def test_existing_adapter_real_protocol_mock_schema_and_rows() -> None:
    transport = FakeTransport()
    item = asset(
        "databricks-sql",
        schema=pa.schema(
            [
                ("id", pa.int64()),
                ("amount", pa.decimal128(24, 12)),
                ("day", pa.date32()),
            ]
        ),
        table=("synthetic", "public", "orders"),
    )
    columns = [("id", "BIGINT"), ("amount", "DECIMAL(24,12)"), ("day", "DATE")]
    transport.enqueue(
        "POST", f"{BASE}/api/2.0/sql/statements", json_body=statement_response(columns, [])
    )
    transport.enqueue(
        "POST",
        f"{BASE}/api/2.0/sql/statements",
        json_body=statement_response(
            columns,
            [["1", "1.123456789012", "2024-02-29"], ["2", None, None]],
        ),
    )
    plugin = DatabricksPlugin(
        [item],
        ResolverConfig(BASE, warehouse_id="synthetic"),
        CredentialReference(id="credential:synthetic-databricks"),
        SyntheticCredentials({"token": "synthetic-protocol-only"}),
        transport=transport,
    )
    assert await plugin.schema("orders") == item.schema
    result = await plugin.execute_validated_fragment(context(), fragment(item), asyncio.Event())
    assert result.to_pylist() == [
        {"id": 1, "amount": Decimal("1.123456789012"), "day": date(2024, 2, 29)},
        {"id": 2, "amount": None, "day": None},
    ]
    assert transport.requests[0].json_body["parameters"][0]["value"] == "0"
    assert await plugin.capabilities("orders")
    transport.assert_drained()
    await plugin.aclose()


async def test_databricks_denied_payload_never_reaches_protocol() -> None:
    transport = FakeTransport()
    item = asset("databricks-sql", table=("synthetic", "public", "orders"))
    plugin = DatabricksPlugin(
        [item],
        ResolverConfig(BASE, warehouse_id="synthetic"),
        CredentialReference(id="credential:synthetic-databricks"),
        SyntheticCredentials({"token": "synthetic-protocol-only"}),
        transport=transport,
    )
    with pytest.raises(ResolverFailure, match="Unsupported"):
        await plugin.execute_validated_fragment(
            context(),
            fragment(
                item,
                OperatorSpec(kind=OperatorKind.AGGREGATE),
            ),
            asyncio.Event(),
        )
    assert not transport.requests
    assert not plugin._active
    await plugin.aclose()


async def test_databricks_cancel_uses_existing_client_cancel_endpoint() -> None:
    transport = FakeTransport()
    item = asset("databricks-sql", table=("synthetic", "public", "orders"))
    transport.enqueue(
        "POST",
        f"{BASE}/api/2.0/sql/statements",
        json_body={
            "statement_id": "synthetic-pending",
            "status": {"state": "PENDING"},
        },
    )
    transport.enqueue(
        "POST", f"{BASE}/api/2.0/sql/statements/synthetic-pending/cancel", json_body={}
    )
    plugin = DatabricksPlugin(
        [item],
        ResolverConfig(BASE, warehouse_id="synthetic", poll_initial_seconds=1, poll_max_seconds=1),
        CredentialReference(id="credential:synthetic-databricks"),
        SyntheticCredentials({"token": "synthetic-protocol-only"}),
        transport=transport,
    )
    task = asyncio.create_task(
        plugin.execute_validated_fragment(
            context(),
            fragment(item),
            asyncio.Event(),
        )
    )
    async with asyncio.timeout(2):
        while not plugin._statements:  # noqa: ASYNC110 - observe the protocol submission boundary
            await asyncio.sleep(0.01)
    await plugin.cancel("synthetic-handle")
    with pytest.raises(asyncio.CancelledError):
        await task
    transport.assert_drained()
    assert not plugin._active and not plugin._statements
    await plugin.aclose()
