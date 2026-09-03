from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field

from .transport import HttpResponse


@dataclass(frozen=True)
class ExpectedRequest:
    method: str
    url: str
    response: HttpResponse


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    json_body: Mapping[str, object] | None
    timeout_seconds: float


@dataclass
class FakeTransport:
    expected: deque[ExpectedRequest] = field(default_factory=deque)
    requests: list[RecordedRequest] = field(default_factory=list)
    closed: bool = False

    def enqueue(
        self,
        method: str,
        url: str,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        content: bytes = b"",
    ) -> None:
        response_headers = dict(headers or {})
        if json_body is not None and "content-type" not in {
            key.lower() for key in response_headers
        }:
            response_headers["content-type"] = "application/json"
        self.expected.append(
            ExpectedRequest(
                method=method.upper(),
                url=url,
                response=HttpResponse(
                    status_code=status_code,
                    headers=response_headers,
                    content=content,
                    json_body=json_body,
                ),
            )
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        if not self.expected:
            raise AssertionError(f"Unexpected request: {method.upper()} {url}")
        expectation = self.expected.popleft()
        if (method.upper(), url) != (expectation.method, expectation.url):
            raise AssertionError(
                f"Expected {expectation.method} {expectation.url}, "
                f"received {method.upper()} {url}"
            )
        self.requests.append(
            RecordedRequest(
                method=method.upper(),
                url=url,
                headers=dict(headers),
                json_body=dict(json_body) if json_body is not None else None,
                timeout_seconds=timeout_seconds,
            )
        )
        return expectation.response

    async def aclose(self) -> None:
        self.closed = True

    def assert_drained(self) -> None:
        if self.expected:
            raise AssertionError(f"{len(self.expected)} expected request(s) were not consumed")
