from __future__ import annotations

import json
from decimal import Decimal

import pytest
from conftest import SyntheticTokenProvider, config

from semantic_data_nexus_databricks.client import StatementExecutionClient
from semantic_data_nexus_databricks.diagnostics import InMemoryDiagnosticSink
from semantic_data_nexus_databricks.exceptions import (
    ResultLimitExceededError,
    StatementFailedError,
    StatementTimeoutError,
)
from semantic_data_nexus_databricks.models import (
    FetchDisposition,
    ResultFormat,
    StatementParameter,
)
from semantic_data_nexus_databricks.testing import FakeTransport

BASE = "https://workspace.example.invalid"


def response(
    state: str,
    *,
    result: dict[str, object] | None = None,
    truncated: bool = False,
) -> dict[str, object]:
    value: dict[str, object] = {
        "statement_id": "statement-test",
        "status": {"state": state},
    }
    if state == "SUCCEEDED":
        value["manifest"] = {
            "format": "JSON_ARRAY",
            "schema": {
                "column_count": 2,
                "columns": [
                    {
                        "name": "region",
                        "position": 0,
                        "type_name": "STRING",
                        "type_text": "STRING",
                    },
                    {
                        "name": "total_amount",
                        "position": 1,
                        "type_name": "DECIMAL",
                        "type_text": "DECIMAL(12,2)",
                    },
                ],
            },
            "total_row_count": 1,
            "total_chunk_count": 1,
            "truncated": truncated,
        }
        value["result"] = result or {
            "chunk_index": 0,
            "row_count": 1,
            "data_array": [["north", "42.50"]],
        }
    return value


async def test_successful_inline_result_and_typed_parameters() -> None:
    transport = FakeTransport()
    transport.enqueue(
        "POST",
        f"{BASE}/api/2.0/sql/statements",
        headers={"x-databricks-request-id": "request-test"},
        json_body=response("SUCCEEDED"),
    )
    client = StatementExecutionClient(config(), SyntheticTokenProvider(), transport=transport)

    result = await client.execute(
        "SELECT region, amount FROM orders WHERE region = :region AND amount >= :minimum",
        (
            StatementParameter.string("region", None),
            StatementParameter.decimal(
                "minimum",
                Decimal("10.50"),
                precision=12,
                scale=2,
            ),
        ),
    )

    assert result.rows == (("north", "42.50"),)
    assert [column.name for column in result.columns] == ["region", "total_amount"]
    body = transport.requests[0].json_body
    assert body is not None
    assert body["parameters"] == [
        {"name": "region", "type": "STRING"},
        {"name": "minimum", "type": "DECIMAL(12,2)", "value": "10.50"},
    ]
    assert body["warehouse_id"] == "warehouse-test"
    assert transport.requests[0].headers["Authorization"] == "Bearer synthetic-test-token"
    assert result.request_ids == ("request-test",)
    transport.assert_drained()


async def test_polling_reaches_success_with_bounded_delays() -> None:
    transport = FakeTransport()
    transport.enqueue("POST", f"{BASE}/api/2.0/sql/statements", json_body=response("PENDING"))
    transport.enqueue(
        "GET",
        f"{BASE}/api/2.0/sql/statements/statement-test",
        json_body=response("RUNNING"),
    )
    transport.enqueue(
        "GET",
        f"{BASE}/api/2.0/sql/statements/statement-test",
        json_body=response("SUCCEEDED"),
    )
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    diagnostics = InMemoryDiagnosticSink()
    client = StatementExecutionClient(
        config(poll_initial_seconds=0.1, poll_max_seconds=0.15),
        SyntheticTokenProvider(),
        transport=transport,
        sleeper=sleeper,
        diagnostics=diagnostics,
    )
    result = await client.execute("SELECT region, amount FROM orders")
    assert result.rows == (("north", "42.50"),)
    assert delays == [0.1, 0.15]
    assert [event.code for event in diagnostics.events] == [
        "DBR_STATEMENT_SUBMITTED",
        "DBR_STATEMENT_POLLED",
        "DBR_STATEMENT_POLLED",
        "DBR_STATEMENT_SUCCEEDED",
    ]


async def test_execution_failure_is_terminal_and_sanitized() -> None:
    transport = FakeTransport()
    failed = response("FAILED")
    failed["status"] = {
        "state": "FAILED",
        "error": {"error_code": "BAD_REQUEST", "message": "sensitive service detail"},
        "sql_state": "42000",
    }
    transport.enqueue("POST", f"{BASE}/api/2.0/sql/statements", json_body=failed)
    client = StatementExecutionClient(config(), SyntheticTokenProvider(), transport=transport)
    with pytest.raises(StatementFailedError) as captured:
        await client.execute("SELECT region FROM orders")
    assert captured.value.error_code == "BAD_REQUEST"
    assert "sensitive service detail" not in str(captured.value)


async def test_deadline_requests_cancellation() -> None:
    transport = FakeTransport()
    transport.enqueue("POST", f"{BASE}/api/2.0/sql/statements", json_body=response("PENDING"))
    transport.enqueue(
        "POST",
        f"{BASE}/api/2.0/sql/statements/statement-test/cancel",
        json_body={},
    )
    moments = iter((0.0, 2.0, 2.0, 2.0))

    def clock() -> float:
        return next(moments)

    client = StatementExecutionClient(
        config(statement_timeout_seconds=1.0),
        SyntheticTokenProvider(),
        transport=transport,
        clock=clock,
    )
    with pytest.raises(StatementTimeoutError):
        await client.execute("SELECT region FROM orders")
    assert transport.requests[-1].url.endswith("/statement-test/cancel")


async def test_deadline_applies_to_terminal_submit_response() -> None:
    transport = FakeTransport()
    transport.enqueue(
        "POST",
        f"{BASE}/api/2.0/sql/statements",
        json_body=response("SUCCEEDED"),
    )
    moments = iter((0.0, 2.0, 2.0))

    def clock() -> float:
        return next(moments)

    client = StatementExecutionClient(
        config(statement_timeout_seconds=1.0),
        SyntheticTokenProvider(),
        transport=transport,
        clock=clock,
    )
    with pytest.raises(StatementTimeoutError):
        await client.execute("SELECT region FROM orders")
    assert len(transport.requests) == 1


async def test_explicit_cancellation() -> None:
    transport = FakeTransport()
    transport.enqueue(
        "POST",
        f"{BASE}/api/2.0/sql/statements/statement-test/cancel",
        json_body={},
    )
    client = StatementExecutionClient(config(), SyntheticTokenProvider(), transport=transport)
    await client.cancel("statement-test")
    assert transport.requests[0].json_body == {}


async def test_external_json_chunks_omit_databricks_authorization() -> None:
    transport = FakeTransport()
    first = response(
        "SUCCEEDED",
        result={
            "chunk_index": 0,
            "external_links": [
                {
                    "chunk_index": 0,
                    "external_link": "https://results.example.invalid/chunk-a?credential=redacted",
                    "http_headers": {"x-synthetic-key": "synthetic-value"},
                }
            ],
        },
    )
    first_result = first["result"]
    assert isinstance(first_result, dict)
    first_links = first_result["external_links"]
    assert isinstance(first_links, list)
    first_link = first_links[0]
    assert isinstance(first_link, dict)
    first_link["next_chunk_index"] = 1
    transport.enqueue("POST", f"{BASE}/api/2.0/sql/statements", json_body=first)
    transport.enqueue(
        "GET",
        "https://results.example.invalid/chunk-a?credential=redacted",
        content=json.dumps([["north", "42.50"]]).encode(),
    )
    transport.enqueue(
        "GET",
        f"{BASE}/api/2.0/sql/statements/statement-test/result/chunks/1",
        json_body={
            "chunk_index": 1,
            "external_links": [
                {
                    "chunk_index": 1,
                    "external_link": "https://results.example.invalid/chunk-b?credential=redacted",
                }
            ],
        },
    )
    transport.enqueue(
        "GET",
        "https://results.example.invalid/chunk-b?credential=redacted",
        content=json.dumps([["south", "7.00"]]).encode(),
    )
    client = StatementExecutionClient(
        config(
            disposition=FetchDisposition.EXTERNAL_LINKS,
            result_format=ResultFormat.JSON_ARRAY,
        ),
        SyntheticTokenProvider(),
        transport=transport,
    )
    result = await client.execute("SELECT region, amount FROM orders")
    assert result.rows == (("north", "42.50"), ("south", "7.00"))
    external_requests = [
        request for request in transport.requests if request.url.startswith("https://results.")
    ]
    assert all("Authorization" not in request.headers for request in external_requests)
    assert external_requests[0].headers == {"x-synthetic-key": "synthetic-value"}


async def test_arrow_external_payload_is_exposed_without_interpretation() -> None:
    transport = FakeTransport()
    succeeded = response(
        "SUCCEEDED",
        result={
            "external_links": [
                {"external_link": "https://results.example.invalid/chunk-arrow"}
            ]
        },
    )
    manifest = succeeded["manifest"]
    assert isinstance(manifest, dict)
    manifest["format"] = "ARROW_STREAM"
    transport.enqueue("POST", f"{BASE}/api/2.0/sql/statements", json_body=succeeded)
    transport.enqueue(
        "GET",
        "https://results.example.invalid/chunk-arrow",
        content=b"synthetic-arrow-stream",
    )
    client = StatementExecutionClient(
        config(
            disposition=FetchDisposition.EXTERNAL_LINKS,
            result_format=ResultFormat.ARROW_STREAM,
        ),
        SyntheticTokenProvider(),
        transport=transport,
    )
    result = await client.execute("SELECT region, amount FROM orders")
    assert result.payloads == (b"synthetic-arrow-stream",)
    assert result.rows == ()


async def test_manifest_truncation_fails_closed() -> None:
    transport = FakeTransport()
    transport.enqueue(
        "POST",
        f"{BASE}/api/2.0/sql/statements",
        json_body=response("SUCCEEDED", truncated=True),
    )
    client = StatementExecutionClient(config(), SyntheticTokenProvider(), transport=transport)
    with pytest.raises(ResultLimitExceededError):
        await client.execute("SELECT region FROM orders")
