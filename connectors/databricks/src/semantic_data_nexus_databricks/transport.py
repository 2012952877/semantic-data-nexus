from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

import httpx


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
        explicit_headers = {key.lower() for key in headers}
        request = self._client.build_request(
            method,
            url,
            headers=headers,
            json=json_body,
            timeout=timeout_seconds,
        )
        # Client defaults can contain credentials. Only caller-supplied headers cross origins.
        for key in tuple(request.headers):
            if key.lower() in self._client.headers and key.lower() not in explicit_headers:
                del request.headers[key]
        if "cookie" not in explicit_headers:
            request.headers.pop("cookie", None)
        response = await self._client.send(request, auth=None, follow_redirects=False)
        content_type = response.headers.get("content-type", "").lower()
        json_response: object | None = None
        if response.content and "json" in content_type:
            json_response = cast(object, response.json())
        return HttpResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
            json_body=json_response,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
