from __future__ import annotations

import httpx

from semantic_data_nexus_databricks.transport import HttpxTransport


async def test_httpx_transport_drops_inherited_credentials() -> None:
    observed_headers: httpx.Headers | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_headers
        observed_headers = request.headers
        return httpx.Response(200, content=b"synthetic")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={
            "Authorization": "Bearer inherited-secret",
            "X-Default-Secret": "inherited-value",
        },
    ) as client:
        transport = HttpxTransport(client)
        await transport.request(
            "GET",
            "https://results.example.invalid/chunk",
            headers={"X-Requested": "synthetic-value"},
            json_body=None,
            timeout_seconds=1,
        )

    assert observed_headers is not None
    assert "Authorization" not in observed_headers
    assert "X-Default-Secret" not in observed_headers
    assert observed_headers["X-Requested"] == "synthetic-value"


async def test_httpx_transport_preserves_explicit_authorization() -> None:
    observed_authorization: str | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_authorization
        observed_authorization = request.headers.get("Authorization")
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer inherited-secret"},
    ) as client:
        transport = HttpxTransport(client)
        await transport.request(
            "GET",
            "https://workspace.example.invalid/api/2.0/sql/statements/statement-test",
            headers={"Authorization": "Bearer explicit-secret"},
            json_body=None,
            timeout_seconds=1,
        )

    assert observed_authorization == "Bearer explicit-secret"
