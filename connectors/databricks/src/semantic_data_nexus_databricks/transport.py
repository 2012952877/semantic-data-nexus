from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

import httpx

from .exceptions import TransportError, TransportTimeoutError


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    json_body: object | None


class AsyncHttpTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        timeout_seconds: float,
    ) -> HttpResponse: ...

    async def aclose(self) -> None: ...


class HttpxTransport:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        request = httpx.Request(
            method,
            url,
            headers=headers,
            json=json_body,
        )
        timeout = {
            "connect": timeout_seconds,
            "read": timeout_seconds,
            "write": timeout_seconds,
            "pool": timeout_seconds,
        }
        request.extensions["timeout"] = timeout
        try:
            response = await self._client.send(request, auth=None, follow_redirects=False)
        except httpx.TimeoutException as error:
            raise TransportTimeoutError("HTTP transport request timed out") from error
        except httpx.HTTPError as error:
            raise TransportError("HTTP transport request failed") from error
        content_type = response.headers.get("content-type", "").lower()
        json_response: object | None = None
        if response.content and "json" in content_type:
            try:
                json_response = cast(object, response.json())
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise TransportError("HTTP response contained invalid JSON") from error
        return HttpResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
            json_body=json_response,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
