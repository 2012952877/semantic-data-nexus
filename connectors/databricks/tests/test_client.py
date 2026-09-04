from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal

import httpx
import pytest
from conftest import SyntheticTokenProvider, config

from semantic_data_nexus_databricks.auth import BearerToken, BearerTokenProvider
from semantic_data_nexus_databricks.client import StatementExecutionClient
from semantic_data_nexus_databricks.diagnostics import InMemoryDiagnosticSink
from semantic_data_nexus_databricks.exceptions import (
    ResultLimitExceededError,
    StatementFailedError,
    StatementTimeoutError,
    TransportTimeoutError,
)
from semantic_data_nexus_databricks.models import (
    FetchDisposition,
    ResultFormat,
    StatementParameter,
)
from semantic_data_nexus_databricks.testing import FakeTransport
from semantic_data_nexus_databricks.transport import HttpResponse, HttpxTransport

BASE = "https://workspace.example.invalid"


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


class AdvancingFakeTransport(FakeTransport):
    def __init__(self, clock: ManualClock, advance_to: float) -> None:
        super().__init__()
        self._clock = clock
        self._advance_to = advance_to

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        response_value = await super().request(
            method,
            url,
            headers=headers,
            json_body=json_body,
            timeout_seconds=timeout_seconds,
        )
        self._clock.now = self._advance_to
        return response_value


class DeadlineTimeoutTransport(FakeTransport):
    def __init__(self, clock: ManualClock, deadline: float) -> None:
        super().__init__()
        self._clock = clock
        self._deadline = deadline

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        if method == "GET":
            self._clock.now = self._deadline
            raise TransportTimeoutError("synthetic transport timeout")
        return await super().request(
            method,
            url,
            headers=headers,
            json_body=json_body,
            timeout_seconds=timeout_seconds,
        )


class ImmediateTimeoutTransport(FakeTransport):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        raise TransportTimeoutError("synthetic immediate timeout")


class FailingCancellationTokenProvider(BearerTokenProvider):
    def __init__(self) -> None:
        self._calls = 0

    async def get_token(self) -> BearerToken:
        self._calls += 1
        if self._calls > 1:
            raise RuntimeError("synthetic provider failure")
        return BearerToken("synthetic-test-token")


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
    assert transport.requests[0].timeout_seconds == 10
    transport.assert_drained()


async def test_submit_uses_timeout_longer_than_server_wait() -> None:
    transport = FakeTransport()
    transport.enqueue(
        "POST",
        f"{BASE}/api/2.0/sql/statements",
        json_body=response("SUCCEEDED"),
    )
    client = StatementExecutionClient(
        config(
            request_timeout_seconds=1,
            statement_timeout_seconds=10,
            api_wait_timeout_seconds=5,
        ),
        SyntheticTokenProvider(),
        transport=transport,
    )
    await client.execute("SELECT region FROM orders")
    assert transport.requests[0].timeout_seconds == 7


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
    clock = ManualClock()

    client = StatementExecutionClient(
        config(
            statement_timeout_seconds=0.1,
            poll_initial_seconds=0.1,
            poll_max_seconds=0.1,
        ),
        SyntheticTokenProvider(),
        transport=transport,
        clock=clock,
        sleeper=clock.sleep,
    )
    with pytest.raises(StatementTimeoutError):
        await client.execute("SELECT region FROM orders")
    assert transport.requests[-1].url.endswith("/statement-test/cancel")
    assert all(request.method != "GET" for request in transport.requests)


async def test_deadline_applies_to_terminal_submit_response() -> None:
    clock = ManualClock()
    transport = AdvancingFakeTransport(clock, advance_to=2.0)
    transport.enqueue(
        "POST",
        f"{BASE}/api/2.0/sql/statements",
        json_body=response("SUCCEEDED"),
    )
    client = StatementExecutionClient(
        config(statement_timeout_seconds=1.0),
        SyntheticTokenProvider(),
        transport=transport,
        clock=clock,
    )
    with pytest.raises(StatementTimeoutError):
        await client.execute("SELECT region FROM orders")
    assert len(transport.requests) == 1


async def test_cancellation_failure_does_not_mask_timeout() -> None:
    transport = FakeTransport()
    transport.enqueue("POST", f"{BASE}/api/2.0/sql/statements", json_body=response("PENDING"))
    transport.enqueue(
        "POST",
        f"{BASE}/api/2.0/sql/statements/statement-test/cancel",
        status_code=503,
        json_body={"error_code": "TEMPORARILY_UNAVAILABLE"},
    )
    clock = ManualClock()
    diagnostics = InMemoryDiagnosticSink()
    client = StatementExecutionClient(
        config(
            statement_timeout_seconds=0.1,
            poll_initial_seconds=0.1,
            poll_max_seconds=0.1,
        ),
        SyntheticTokenProvider(),
        transport=transport,
        clock=clock,
        sleeper=clock.sleep,
        diagnostics=diagnostics,
    )
    with pytest.raises(StatementTimeoutError):
        await client.execute("SELECT region FROM orders")
    assert [event.code for event in diagnostics.events][-2:] == [
        "DBR_CANCEL_FAILED",
        "DBR_STATEMENT_TIMEOUT",
    ]


async def test_unexpected_cancellation_failure_does_not_mask_timeout() -> None:
    transport = FakeTransport()
    transport.enqueue("POST", f"{BASE}/api/2.0/sql/statements", json_body=response("PENDING"))
    clock = ManualClock()
    diagnostics = InMemoryDiagnosticSink()
    client = StatementExecutionClient(
        config(
            statement_timeout_seconds=0.1,
            poll_initial_seconds=0.1,
            poll_max_seconds=0.1,
        ),
        FailingCancellationTokenProvider(),
        transport=transport,
        clock=clock,
        sleeper=clock.sleep,
        diagnostics=diagnostics,
    )
    with pytest.raises(StatementTimeoutError):
        await client.execute("SELECT region FROM orders")
    assert diagnostics.events[-2].code == "DBR_CANCEL_FAILED"
    assert diagnostics.events[-1].code == "DBR_STATEMENT_TIMEOUT"


@pytest.mark.parametrize("content", [b"{", b"\xff"])
async def test_malformed_cancellation_response_does_not_mask_timeout(
    content: bytes,
) -> None:
    call_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if request.url.path.endswith("/cancel"):
            return httpx.Response(
                200,
                content=content,
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200,
            json=response("PENDING"),
            headers={"content-type": "application/json"},
        )

    clock = ManualClock()
    diagnostics = InMemoryDiagnosticSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = StatementExecutionClient(
            config(
                statement_timeout_seconds=0.1,
                poll_initial_seconds=0.1,
                poll_max_seconds=0.1,
            ),
            SyntheticTokenProvider(),
            transport=HttpxTransport(http_client),
            clock=clock,
            sleeper=clock.sleep,
            diagnostics=diagnostics,
        )
        with pytest.raises(StatementTimeoutError):
            await client.execute("SELECT region FROM orders")

    assert call_count == 2
    assert [event.code for event in diagnostics.events][-2:] == [
        "DBR_CANCEL_FAILED",
        "DBR_STATEMENT_TIMEOUT",
    ]


async def test_deadline_limited_transport_timeout_uses_statement_timeout() -> None:
    clock = ManualClock()
    transport = DeadlineTimeoutTransport(clock, deadline=0.2)
    transport.enqueue("POST", f"{BASE}/api/2.0/sql/statements", json_body=response("PENDING"))
    transport.enqueue(
        "POST",
        f"{BASE}/api/2.0/sql/statements/statement-test/cancel",
        json_body={},
    )
    diagnostics = InMemoryDiagnosticSink()
    client = StatementExecutionClient(
        config(
            statement_timeout_seconds=0.2,
            poll_initial_seconds=0.1,
            poll_max_seconds=0.1,
        ),
        SyntheticTokenProvider(),
        transport=transport,
        clock=clock,
        sleeper=clock.sleep,
        diagnostics=diagnostics,
    )
    with pytest.raises(StatementTimeoutError):
        await client.execute("SELECT region FROM orders")
    assert transport.requests[-1].url.endswith("/statement-test/cancel")
    assert diagnostics.events[-1].code == "DBR_STATEMENT_TIMEOUT"


async def test_immediate_transport_timeout_keeps_transport_identity() -> None:
    client = StatementExecutionClient(
        config(request_timeout_seconds=1, statement_timeout_seconds=1),
        SyntheticTokenProvider(),
        transport=ImmediateTimeoutTransport(),
        clock=ManualClock(),
    )
    with pytest.raises(TransportTimeoutError):
        await client.execute("SELECT region FROM orders")


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


async def test_external_link_batch_continues_after_highest_consumed_chunk() -> None:
    transport = FakeTransport()
    first = response(
        "SUCCEEDED",
        result={
            "chunk_index": 0,
            "external_links": [
                {
                    "chunk_index": 0,
                    "external_link": "https://results.example.invalid/chunk-a",
                    "next_chunk_index": 1,
                },
                {
                    "chunk_index": 1,
                    "external_link": "https://results.example.invalid/chunk-b",
                    "next_chunk_index": 2,
                },
            ],
        },
    )
    transport.enqueue("POST", f"{BASE}/api/2.0/sql/statements", json_body=first)
    transport.enqueue(
        "GET",
        "https://results.example.invalid/chunk-a",
        content=json.dumps([["north", "42.50"]]).encode(),
    )
    transport.enqueue(
        "GET",
        "https://results.example.invalid/chunk-b",
        content=json.dumps([["south", "7.00"]]).encode(),
    )
    transport.enqueue(
        "GET",
        f"{BASE}/api/2.0/sql/statements/statement-test/result/chunks/2",
        json_body={
            "chunk_index": 2,
            "external_links": [
                {
                    "chunk_index": 2,
                    "external_link": "https://results.example.invalid/chunk-c",
                }
            ],
        },
    )
    transport.enqueue(
        "GET",
        "https://results.example.invalid/chunk-c",
        content=json.dumps([["west", "9.00"]]).encode(),
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

    assert result.rows == (
        ("north", "42.50"),
        ("south", "7.00"),
        ("west", "9.00"),
    )
    assert not any(request.url.endswith("/result/chunks/1") for request in transport.requests)
    transport.assert_drained()


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
