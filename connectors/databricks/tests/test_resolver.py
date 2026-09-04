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
        "SELECT region FROM orders WHERE region = ?",
        "WITH changed AS (INSERT INTO orders SELECT * FROM staged) SELECT * FROM changed",
        "WITH changed AS (UPDATE orders SET amount = 0) SELECT * FROM changed",
        "WITH changed AS (DELETE FROM orders RETURNING *) SELECT * FROM changed",
        (
            "WITH changed AS (MERGE INTO orders USING staged ON orders.id = staged.id "
            "WHEN MATCHED THEN UPDATE SET amount = staged.amount) SELECT * FROM changed"
        ),
        "WITH changed AS (COPY INTO orders FROM '/synthetic') SELECT * FROM changed",
        "WITH changed AS (USE CATALOG alternate) SELECT * FROM changed",
        "WITH changed AS (REFRESH TABLE cached) SELECT * FROM changed",
        (
            "WITH changed AS (ANALYZE TABLE orders COMPUTE STATISTICS) "
            "SELECT * FROM changed"
        ),
        "SELECT region FROM orders WHERE id = $1",
        "SELECT region FROM orders WHERE id = @1",
        "SELECT * INTO persisted_copy FROM source_table",
        "SELECT http_request('DELETE', 'https://service.example.invalid/resource')",
        "SELECT synthetic_sql_udf(amount) FROM orders",
        "SELECT synthetic.python_udf(amount) FROM orders",
        "SELECT DATE_TO_DATE_STR(order_date) FROM orders",
        "SELECT TS_OR_DS_ADD(order_date, 1) FROM orders",
        "SELECT TS_OR_DS_TO_DATE(order_date) FROM orders",
        "SELECT synthetic.SUM(amount) FROM orders",
        "SELECT `SUM`(amount) FROM orders",
        "SELECT CURRENT_USER",
        "SELECT MOD(5, 2)",
        "SELECT ELEMENT_AT(items, 1) FROM orders",
        "SELECT LIKE(region, 'n%') FROM orders",
        "SELECT SUM(TRY_CAST(amount AS INT)) FROM orders",
        "SELECT ABS(CEIL(amount)) FROM orders",
        "SELECT CURRENT_DATE, CURDATE()",
        "SELECT ISNULL(amount) FROM orders",
        "SELECT TABLE(1)",
        "SELECT synthetic.CURRENT_DATE()",
        "SELECT catalog.synthetic.CURRENT_DATE()",
        "SELECT CURDATE",
        "SELECT ABS(CURDATE)",
    ],
)
def test_unsafe_or_multi_statement_sql_is_rejected(sql: str) -> None:
    with pytest.raises(UnsafeStatementError):
        validate_fragment(PhysicalSourceFragment(source_name="demo_sales", sql=sql))


@pytest.mark.parametrize(
    "sql",
    [
        r"SELECT 'safe\' ; DELETE FROM orders' AS note",
        "SELECT 'DELETE;still text' AS note FROM orders;",
        "SELECT region FROM orders /* DELETE FROM orders */",
        "SELECT region FROM orders; -- trailing comment",
        "WITH safe_rows AS (SELECT region FROM orders) SELECT * FROM safe_rows",
        "WITH safe_rows(region) AS (SELECT region FROM orders) SELECT region FROM safe_rows",
        "SELECT * FROM (SELECT region FROM orders) AS safe_rows(region)",
    ],
)
def test_literals_comments_and_read_only_ctes_are_safe(sql: str) -> None:
    validate_fragment(
        PhysicalSourceFragment(
            source_name="demo_sales",
            sql=sql,
        )
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(amount), COUNT(*), AVG(amount), MIN(amount), MAX(amount) FROM orders",
        (
            "SELECT CURRENT_DATE(), DATE_ADD(order_date, 1), DATE_SUB(order_date, 1), "
            "DATEDIFF(CURRENT_DATE(), order_date), DATE_TRUNC('month', order_date), "
            "YEAR(order_date), MONTH(order_date), DAY(order_date) FROM orders"
        ),
        (
            "SELECT LOWER(region), UPPER(region), COALESCE(region, 'unknown'), "
            "ABS(amount), ROUND(amount, 2), CAST(amount AS STRING) FROM orders"
        ),
    ],
)
def test_safe_m0_functions_are_allowed(sql: str) -> None:
    validate_fragment(PhysicalSourceFragment(source_name="demo_sales", sql=sql))


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
