from __future__ import annotations

import os

import pytest

from semantic_data_nexus_databricks import (
    DatabricksResolver,
    EnvironmentPatProvider,
    PhysicalSourceFragment,
    ResolverConfig,
    StatementExecutionClient,
)

_REQUIRED = (
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_WAREHOUSE_ID",
    "DATABRICKS_SMOKE_STATEMENT",
)


@pytest.mark.live
async def test_live_read_only_statement() -> None:
    if not all(os.environ.get(name) for name in _REQUIRED):
        pytest.skip("explicit live smoke environment is not configured")

    config = ResolverConfig(
        workspace_host=os.environ["DATABRICKS_HOST"],
        warehouse_id=os.environ["DATABRICKS_WAREHOUSE_ID"],
        statement_timeout_seconds=20,
        request_timeout_seconds=10,
        row_limit=10,
        byte_limit=1024 * 1024,
    )
    client = StatementExecutionClient(config, EnvironmentPatProvider())
    try:
        result = await DatabricksResolver(client).resolve(
            PhysicalSourceFragment(
                source_name="live_read_only",
                sql=os.environ["DATABRICKS_SMOKE_STATEMENT"],
            )
        )
        assert len(result.rows) <= 10
    finally:
        await client.aclose()
