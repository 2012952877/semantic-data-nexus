from __future__ import annotations

import pytest
from conftest import SyntheticTokenProvider, config

from semantic_data_nexus_databricks.client import StatementExecutionClient
from semantic_data_nexus_databricks.exceptions import UnsafeStatementError
from semantic_data_nexus_databricks.models import PhysicalSourceFragment, StatementParameter
from semantic_data_nexus_databricks.resolver import DatabricksResolver, validate_fragment
from semantic_data_nexus_databricks.testing import FakeTransport

BASE = "https://workspace.example.invalid"


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "SELECT region FROM orders; SELECT amount FROM orders",
        "WITH changed AS (UPDATE orders SET amount = 0) SELECT * FROM changed",
        "SELECT region FROM orders WHERE region = ?",
    ],
)
def test_unsafe_or_multi_statement_sql_is_rejected(sql: str) -> None:
    with pytest.raises(UnsafeStatementError):
        validate_fragment(PhysicalSourceFragment(source_name="demo_sales", sql=sql))


def test_semicolon_and_keyword_inside_literal_are_safe() -> None:
    validate_fragment(
        PhysicalSourceFragment(
            source_name="demo_sales",
            sql="SELECT 'DELETE;still text' AS note FROM orders;",
        )
    )


def test_parameter_markers_must_match() -> None:
    with pytest.raises(UnsafeStatementError, match="must match"):
        validate_fragment(
            PhysicalSourceFragment(
                source_name="demo_sales",
                sql="SELECT region FROM orders WHERE region = :region",
            )
        )


async def test_resolver_returns_normalized_rows_and_non_secret_lineage() -> None:
    transport = FakeTransport()
    transport.enqueue(
        "POST",
        f"{BASE}/api/2.0/sql/statements",
        json_body={
            "statement_id": "statement-test",
            "status": {"state": "SUCCEEDED"},
            "manifest": {
                "format": "JSON_ARRAY",
                "schema": {
                    "columns": [
                        {
                            "name": "region",
                            "position": 0,
                            "type_name": "STRING",
                            "type_text": "STRING",
                        }
                    ]
                },
                "truncated": False,
            },
            "result": {"data_array": [["north"]]},
        },
    )
    client = StatementExecutionClient(config(), SyntheticTokenProvider(), transport=transport)
    resolver = DatabricksResolver(client)
    result = await resolver.resolve(
        PhysicalSourceFragment(
            source_name="demo_sales",
            sql="SELECT region FROM orders WHERE region = :region",
            parameters=(StatementParameter.string("region", "north"),),
        )
    )
    assert result.rows == (("north",),)
    assert result.lineage.resolver == "azure_databricks_statement_execution"
    assert result.lineage.statement_id == "statement-test"
    assert len(result.lineage.physical_fragment_sha256) == 64
    lineage_text = repr(result.lineage)
    assert "workspace.example.invalid" not in lineage_text
    assert "synthetic-test-token" not in lineage_text
