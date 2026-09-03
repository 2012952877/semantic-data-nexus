from __future__ import annotations

import math
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .exceptions import ConfigurationError
from .models import FetchDisposition, ResultFormat, WaitTimeoutAction

_HTTP_PATH = re.compile(r"^/sql/1[.]0/warehouses/(?P<warehouse_id>[A-Za-z0-9_-]+)$")
_WAREHOUSE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SUBMIT_TIMEOUT_OVERHEAD_SECONDS = 2.0


def _normalize_host(value: str) -> str:
    candidate = value.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() != "https":
        raise ConfigurationError("workspace_host must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ConfigurationError("workspace_host must be an HTTPS origin without user information")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ConfigurationError("workspace_host must not contain a path, query, or fragment")
    if ":" in parsed.hostname:
        raise ConfigurationError("workspace_host must use a DNS hostname, not an IPv6 literal")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ConfigurationError("workspace_host contains an invalid port")
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"https://{host}{port}"


def _warehouse_from_http_path(http_path: str) -> tuple[str, str]:
    normalized = "/" + http_path.strip().strip("/")
    match = _HTTP_PATH.fullmatch(normalized)
    if match is None:
        raise ConfigurationError(
            "http_path must match /sql/1.0/warehouses/{warehouse_id}"
        )
    return normalized, match.group("warehouse_id")


@dataclass(frozen=True)
class ResolverConfig:
    workspace_host: str
    warehouse_id: str | None = None
    http_path: str | None = None
    catalog: str = "demo_sales"
    schema: str = "analytics"
    request_timeout_seconds: float = 10.0
    statement_timeout_seconds: float = 30.0
    poll_initial_seconds: float = 0.1
    poll_max_seconds: float = 2.0
    api_wait_timeout_seconds: int = 0
    on_wait_timeout: WaitTimeoutAction = WaitTimeoutAction.CONTINUE
    disposition: FetchDisposition = FetchDisposition.INLINE
    result_format: ResultFormat = ResultFormat.JSON_ARRAY
    row_limit: int = 1_000
    byte_limit: int = 10 * 1024 * 1024
    cancel_on_timeout: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_host", _normalize_host(self.workspace_host))

        warehouse_id = self.warehouse_id.strip() if self.warehouse_id else None
        http_path = self.http_path
        path_warehouse_id: str | None = None
        if http_path is not None:
            http_path, path_warehouse_id = _warehouse_from_http_path(http_path)
            object.__setattr__(self, "http_path", http_path)

        if warehouse_id is None:
            warehouse_id = path_warehouse_id
        if warehouse_id is None:
            raise ConfigurationError("warehouse_id or http_path is required")
        if _WAREHOUSE_ID.fullmatch(warehouse_id) is None:
            raise ConfigurationError("warehouse_id contains unsupported characters")
        if path_warehouse_id is not None and path_warehouse_id != warehouse_id:
            raise ConfigurationError("warehouse_id does not match http_path")
        object.__setattr__(self, "warehouse_id", warehouse_id)

        if not self.catalog.strip() or not self.schema.strip():
            raise ConfigurationError("catalog and schema defaults must be non-empty")
        durations = (
            self.request_timeout_seconds,
            self.statement_timeout_seconds,
            self.poll_initial_seconds,
            self.poll_max_seconds,
        )
        if not all(math.isfinite(value) and value > 0 for value in durations):
            raise ConfigurationError("request and statement timeouts must be positive")
        if self.poll_initial_seconds > self.poll_max_seconds:
            raise ConfigurationError("poll_initial_seconds cannot exceed poll_max_seconds")
        if self.api_wait_timeout_seconds != 0 and not 5 <= self.api_wait_timeout_seconds <= 50:
            raise ConfigurationError("api_wait_timeout_seconds must be 0 or between 5 and 50")
        if (
            self.api_wait_timeout_seconds
            and self.statement_timeout_seconds
            <= self.api_wait_timeout_seconds + _SUBMIT_TIMEOUT_OVERHEAD_SECONDS
        ):
            raise ConfigurationError(
                "statement_timeout_seconds must include API wait timeout overhead"
            )
        if self.row_limit <= 0 or self.byte_limit <= 0:
            raise ConfigurationError("row_limit and byte_limit must be positive")
        if (
            self.disposition is FetchDisposition.INLINE
            and self.result_format is not ResultFormat.JSON_ARRAY
        ):
            raise ConfigurationError("INLINE disposition supports only JSON_ARRAY")

    @property
    def api_wait_timeout(self) -> str:
        return f"{self.api_wait_timeout_seconds}s"

    @property
    def submit_request_timeout_seconds(self) -> float:
        return max(
            self.request_timeout_seconds,
            self.api_wait_timeout_seconds + _SUBMIT_TIMEOUT_OVERHEAD_SECONDS,
        )
