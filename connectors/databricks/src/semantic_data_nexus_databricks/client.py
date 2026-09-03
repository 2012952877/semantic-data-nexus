from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import cast
from urllib.parse import urlsplit

from .auth import BearerTokenProvider
from .config import ResolverConfig
from .diagnostics import Diagnostic, DiagnosticLevel, DiagnosticSink
from .exceptions import (
    ProtocolError,
    ResultDecodeError,
    ResultLimitExceededError,
    StatementCanceledError,
    StatementClosedError,
    StatementFailedError,
    StatementTimeoutError,
    TransportHttpError,
)
from .models import (
    CellValue,
    ResultColumn,
    ResultFormat,
    ResultManifest,
    ServiceError,
    StatementExecutionResult,
    StatementParameter,
    StatementResponse,
    StatementState,
    StatementStatus,
)
from .transport import AsyncHttpTransport, HttpResponse, HttpxTransport

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]

_REQUEST_ID_HEADERS = (
    "x-databricks-request-id",
    "x-request-id",
    "request-id",
)


class StatementExecutionClient:
    def __init__(
        self,
        config: ResolverConfig,
        auth: BearerTokenProvider,
        *,
        transport: AsyncHttpTransport | None = None,
        diagnostics: DiagnosticSink | None = None,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self._config = config
        self._auth = auth
        self._transport = transport or HttpxTransport()
        self._diagnostics = diagnostics
        self._clock = clock
        self._sleep = sleeper

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def cancel(self, statement_id: str) -> None:
        await self._api_request(
            "POST",
            f"/api/2.0/sql/statements/{statement_id}/cancel",
            json_body={},
        )
        self._emit(
            "DBR_CANCEL_REQUESTED",
            DiagnosticLevel.INFO,
            "Statement cancellation was requested",
            statement_id=statement_id,
        )

    async def execute(
        self,
        statement: str,
        parameters: tuple[StatementParameter, ...] = (),
    ) -> StatementExecutionResult:
        started = self._clock()
        response = await self._submit(statement, parameters)
        statement_id = response.statement_id
        request_ids = [response.request_id] if response.request_id else []
        delay = self._config.poll_initial_seconds
        deadline = started + self._config.statement_timeout_seconds
        if self._clock() >= deadline:
            await self._handle_timeout(
                statement_id,
                started,
                cancel=response.status.state in (StatementState.PENDING, StatementState.RUNNING),
            )

        while response.status.state in (StatementState.PENDING, StatementState.RUNNING):
            now = self._clock()
            if now >= deadline:
                await self._handle_timeout(statement_id, started)
            await self._sleep(min(delay, max(0.0, deadline - now)))
            response = await self._get_statement(statement_id)
            if self._clock() >= deadline:
                await self._handle_timeout(
                    statement_id,
                    started,
                    cancel=response.status.state
                    in (StatementState.PENDING, StatementState.RUNNING),
                )
            if response.request_id:
                request_ids.append(response.request_id)
            self._emit(
                "DBR_STATEMENT_POLLED",
                DiagnosticLevel.DEBUG,
                "Statement status was polled",
                duration_ms=self._elapsed_ms(started),
                request_id=response.request_id,
                statement_id=statement_id,
                state=response.status.state.value,
            )
            delay = min(delay * 2, self._config.poll_max_seconds)

        if response.status.state is StatementState.FAILED:
            error_code = response.status.error.error_code if response.status.error else None
            self._emit(
                "DBR_STATEMENT_FAILED",
                DiagnosticLevel.ERROR,
                "Statement execution failed",
                duration_ms=self._elapsed_ms(started),
                request_id=response.request_id,
                statement_id=statement_id,
                state=response.status.state.value,
            )
            raise StatementFailedError(statement_id, error_code, response.status.sql_state)
        if response.status.state is StatementState.CANCELED:
            raise StatementCanceledError(statement_id)
        if response.status.state is StatementState.CLOSED:
            raise StatementClosedError(statement_id)
        if response.status.state is not StatementState.SUCCEEDED:
            raise ProtocolError(f"Unsupported terminal state {response.status.state.value}")

        result = await self._collect_result(response, started, request_ids)
        self._emit(
            "DBR_STATEMENT_SUCCEEDED",
            DiagnosticLevel.INFO,
            "Statement execution succeeded",
            duration_ms=result.elapsed_ms,
            request_id=response.request_id,
            statement_id=statement_id,
            state=response.status.state.value,
        )
        return result

    async def _handle_timeout(
        self,
        statement_id: str,
        started: float,
        *,
        cancel: bool = True,
    ) -> None:
        if cancel and self._config.cancel_on_timeout:
            await self.cancel(statement_id)
        self._emit(
            "DBR_STATEMENT_TIMEOUT",
            DiagnosticLevel.ERROR,
            "Statement execution exceeded its deadline",
            duration_ms=self._elapsed_ms(started),
            statement_id=statement_id,
        )
        raise StatementTimeoutError(statement_id)

    async def _submit(
        self,
        statement: str,
        parameters: tuple[StatementParameter, ...],
    ) -> StatementResponse:
        body: dict[str, object] = {
            "statement": statement,
            "warehouse_id": self._config.warehouse_id,
            "catalog": self._config.catalog,
            "schema": self._config.schema,
            "parameters": [parameter.to_api_dict() for parameter in parameters],
            "row_limit": self._config.row_limit,
            "byte_limit": self._config.byte_limit,
            "disposition": self._config.disposition.value,
            "format": self._config.result_format.value,
            "wait_timeout": self._config.api_wait_timeout,
            "on_wait_timeout": self._config.on_wait_timeout.value,
        }
        raw, request_id = await self._api_request(
            "POST",
            "/api/2.0/sql/statements",
            json_body=body,
        )
        response = self._parse_statement_response(raw, request_id)
        self._emit(
            "DBR_STATEMENT_SUBMITTED",
            DiagnosticLevel.INFO,
            "Statement was submitted",
            request_id=request_id,
            statement_id=response.statement_id,
            state=response.status.state.value,
        )
        return response

    async def _get_statement(self, statement_id: str) -> StatementResponse:
        raw, request_id = await self._api_request(
            "GET",
            f"/api/2.0/sql/statements/{statement_id}",
        )
        return self._parse_statement_response(raw, request_id)

    async def _get_chunk(self, statement_id: str, chunk_index: int) -> dict[str, object]:
        raw, _ = await self._api_request(
            "GET",
            f"/api/2.0/sql/statements/{statement_id}/result/chunks/{chunk_index}",
        )
        return raw

    async def _get_internal_chunk(self, internal_link: str) -> dict[str, object]:
        parsed = urlsplit(internal_link)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith(
            "/api/2.0/sql/statements/"
        ):
            raise ProtocolError("next_chunk_internal_link is not a workspace API path")
        raw, _ = await self._api_request(
            "GET",
            parsed.path + (f"?{parsed.query}" if parsed.query else ""),
        )
        return raw

    async def _collect_result(
        self,
        response: StatementResponse,
        started: float,
        request_ids: list[str],
    ) -> StatementExecutionResult:
        manifest = response.manifest
        if manifest is None:
            raise ProtocolError("Successful statement response has no result manifest")
        if manifest.truncated:
            raise ResultLimitExceededError(
                "Databricks truncated the result at the configured row or byte limit"
            )

        rows: list[tuple[CellValue, ...]] = []
        payloads: list[bytes] = []
        byte_count = 0
        current = response.result
        seen_chunks: set[int] = set()

        while current is not None:
            (
                chunk_rows,
                chunk_payloads,
                measured_bytes,
                external_next_index,
                external_next_internal,
            ) = await self._consume_chunk(
                current,
                manifest.result_format,
            )
            if self._clock() >= started + self._config.statement_timeout_seconds:
                await self._handle_timeout(response.statement_id, started, cancel=False)
            rows.extend(chunk_rows)
            payloads.extend(chunk_payloads)
            byte_count += measured_bytes
            if len(rows) > self._config.row_limit or byte_count > self._config.byte_limit:
                raise ResultLimitExceededError("Downloaded result exceeds the configured boundary")

            next_index = self._optional_int(current.get("next_chunk_index"))
            next_internal = self._optional_str(current.get("next_chunk_internal_link"))
            if (
                next_index is not None
                and external_next_index is not None
                and next_index != external_next_index
            ):
                raise ProtocolError("Result chunk has conflicting next_chunk_index values")
            if (
                next_internal is not None
                and external_next_internal is not None
                and next_internal != external_next_internal
            ):
                raise ProtocolError(
                    "Result chunk has conflicting next_chunk_internal_link values"
                )
            next_index = next_index if next_index is not None else external_next_index
            next_internal = (
                next_internal if next_internal is not None else external_next_internal
            )
            if next_index is not None:
                if next_index in seen_chunks:
                    raise ProtocolError("Result chunk sequence contains a cycle")
                seen_chunks.add(next_index)
                current = await self._get_chunk(response.statement_id, next_index)
            elif next_internal is not None:
                current = await self._get_internal_chunk(next_internal)
            else:
                current = None

        return StatementExecutionResult(
            statement_id=response.statement_id,
            columns=manifest.columns,
            rows=tuple(rows),
            payloads=tuple(payloads),
            result_format=manifest.result_format,
            elapsed_ms=self._elapsed_ms(started),
            request_ids=tuple(request_ids),
        )

    async def _consume_chunk(
        self,
        chunk: Mapping[str, object],
        result_format: ResultFormat,
    ) -> tuple[
        list[tuple[CellValue, ...]],
        list[bytes],
        int,
        int | None,
        str | None,
    ]:
        rows = self._parse_rows(chunk.get("data_array"))
        payloads: list[bytes] = []
        byte_count = len(json.dumps(rows, separators=(",", ":")).encode()) if rows else 0
        next_index: int | None = None
        next_internal: str | None = None
        external_links = chunk.get("external_links")
        if external_links is not None:
            if not isinstance(external_links, list):
                raise ProtocolError("external_links must be an array")
            for raw_link in external_links:
                payload, link_next_index, link_next_internal = await self._fetch_external_link(
                    raw_link
                )
                if (
                    next_index is not None
                    and link_next_index is not None
                    and next_index != link_next_index
                ):
                    raise ProtocolError("External links have conflicting continuation indexes")
                if (
                    next_internal is not None
                    and link_next_internal is not None
                    and next_internal != link_next_internal
                ):
                    raise ProtocolError("External links have conflicting continuation paths")
                next_index = next_index if next_index is not None else link_next_index
                next_internal = (
                    next_internal if next_internal is not None else link_next_internal
                )
                byte_count += len(payload)
                if result_format is ResultFormat.JSON_ARRAY:
                    rows.extend(self._decode_external_json(payload))
                else:
                    payloads.append(payload)
        return rows, payloads, byte_count, next_index, next_internal

    async def _fetch_external_link(
        self,
        raw_link: object,
    ) -> tuple[bytes, int | None, str | None]:
        if not isinstance(raw_link, dict):
            raise ProtocolError("external link must be an object")
        url = raw_link.get("external_link")
        if not isinstance(url, str) or urlsplit(url).scheme.lower() != "https":
            raise ProtocolError("external link must be an HTTPS URL")
        raw_headers = raw_link.get("http_headers", {})
        if not isinstance(raw_headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in raw_headers.items()
        ):
            raise ProtocolError("external link headers must be string pairs")
        headers = cast(dict[str, str], raw_headers)
        if any(key.lower() == "authorization" for key in headers):
            raise ProtocolError("external link must not receive an Authorization header")
        response = await self._transport.request(
            "GET",
            url,
            headers=headers,
            json_body=None,
            timeout_seconds=self._config.request_timeout_seconds,
        )
        self._ensure_success(response)
        self._emit(
            "DBR_EXTERNAL_CHUNK_FETCHED",
            DiagnosticLevel.DEBUG,
            "External result chunk was fetched",
        )
        return (
            response.content,
            self._optional_int(raw_link.get("next_chunk_index")),
            self._optional_str(raw_link.get("next_chunk_internal_link")),
        )

    @staticmethod
    def _decode_external_json(payload: bytes) -> list[tuple[CellValue, ...]]:
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ResultDecodeError("External JSON result could not be decoded") from error
        return StatementExecutionClient._parse_rows(value)

    @staticmethod
    def _parse_rows(value: object) -> list[tuple[CellValue, ...]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ProtocolError("JSON_ARRAY result data must be an array")
        rows: list[tuple[CellValue, ...]] = []
        for row in value:
            if not isinstance(row, list) or not all(
                cell is None or isinstance(cell, str) for cell in row
            ):
                raise ProtocolError("JSON_ARRAY rows must contain only strings or nulls")
            rows.append(tuple(row))
        return rows

    async def _api_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
    ) -> tuple[dict[str, object], str | None]:
        token = await self._auth.get_token()
        headers = {
            "Accept": "application/json",
            **token.authorization_header(),
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response = await self._transport.request(
            method,
            f"{self._config.workspace_host}{path}",
            headers=headers,
            json_body=json_body,
            timeout_seconds=self._config.request_timeout_seconds,
        )
        self._ensure_success(response)
        request_id = self._request_id(response.headers)
        if response.json_body is None and not response.content:
            return {}, request_id
        if not isinstance(response.json_body, dict):
            raise ProtocolError("Databricks API response must be a JSON object")
        return cast(dict[str, object], response.json_body), request_id

    @staticmethod
    def _ensure_success(response: HttpResponse) -> None:
        if not 200 <= response.status_code < 300:
            raise TransportHttpError(
                response.status_code,
                StatementExecutionClient._request_id(response.headers),
            )

    @staticmethod
    def _request_id(headers: Mapping[str, str]) -> str | None:
        lowered = {key.lower(): value for key, value in headers.items()}
        return next(
            (lowered[name] for name in _REQUEST_ID_HEADERS if lowered.get(name)),
            None,
        )

    @staticmethod
    def _parse_statement_response(
        raw: Mapping[str, object],
        request_id: str | None,
    ) -> StatementResponse:
        statement_id = raw.get("statement_id")
        status = raw.get("status")
        if not isinstance(statement_id, str) or not statement_id:
            raise ProtocolError("Statement response is missing statement_id")
        if not isinstance(status, dict):
            raise ProtocolError("Statement response is missing status")
        state_value = status.get("state")
        if not isinstance(state_value, str):
            raise ProtocolError("Statement status is missing state")
        try:
            state = StatementState(state_value)
        except ValueError as error:
            raise ProtocolError(f"Unknown statement state {state_value!r}") from error
        error_value = status.get("error")
        error_code: str | None = None
        if isinstance(error_value, dict):
            raw_code = error_value.get("error_code")
            error_code = raw_code if isinstance(raw_code, str) else None
        sql_state = status.get("sql_state")
        parsed_status = StatementStatus(
            state=state,
            error=None if error_code is None else ServiceError(error_code=error_code),
            sql_state=sql_state if isinstance(sql_state, str) else None,
        )
        manifest_value = raw.get("manifest")
        manifest = (
            StatementExecutionClient._parse_manifest(manifest_value)
            if manifest_value is not None
            else None
        )
        result = raw.get("result")
        if result is not None and not isinstance(result, dict):
            raise ProtocolError("Statement result must be an object")
        return StatementResponse(
            statement_id=statement_id,
            status=parsed_status,
            manifest=manifest,
            result=cast(dict[str, object] | None, result),
            request_id=request_id,
        )

    @staticmethod
    def _parse_manifest(value: object) -> ResultManifest:
        if not isinstance(value, dict):
            raise ProtocolError("Result manifest must be an object")
        format_value = value.get("format", ResultFormat.JSON_ARRAY.value)
        if not isinstance(format_value, str):
            raise ProtocolError("Result manifest format must be a string")
        try:
            result_format = ResultFormat(format_value)
        except ValueError as error:
            raise ProtocolError(f"Unknown result format {format_value!r}") from error
        schema = value.get("schema")
        columns: list[ResultColumn] = []
        if isinstance(schema, dict):
            raw_columns = schema.get("columns", [])
            if not isinstance(raw_columns, list):
                raise ProtocolError("Result schema columns must be an array")
            for index, raw_column in enumerate(raw_columns):
                if not isinstance(raw_column, dict):
                    raise ProtocolError("Result column must be an object")
                name = raw_column.get("name")
                type_name = raw_column.get("type_name")
                type_text = raw_column.get("type_text")
                position = raw_column.get("position", index)
                if (
                    not isinstance(name, str)
                    or not isinstance(type_name, str)
                    or not isinstance(type_text, str)
                    or not isinstance(position, int)
                    or isinstance(position, bool)
                ):
                    raise ProtocolError("Result column metadata is incomplete")
                columns.append(
                    ResultColumn(
                        name=name,
                        position=position,
                        type_name=type_name,
                        type_text=type_text,
                    )
                )
        return ResultManifest(
            columns=tuple(columns),
            result_format=result_format,
            total_row_count=StatementExecutionClient._optional_int(
                value.get("total_row_count")
            ),
            total_byte_count=StatementExecutionClient._optional_int(
                value.get("total_byte_count")
            ),
            total_chunk_count=StatementExecutionClient._optional_int(
                value.get("total_chunk_count")
            ),
            truncated=value.get("truncated") is True,
        )

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProtocolError("Expected an integer field")
        return value

    @staticmethod
    def _optional_str(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ProtocolError("Expected a string field")
        return value

    def _elapsed_ms(self, started: float) -> int:
        return max(0, round((self._clock() - started) * 1000))

    def _emit(
        self,
        code: str,
        level: DiagnosticLevel,
        message: str,
        *,
        duration_ms: int | None = None,
        request_id: str | None = None,
        statement_id: str | None = None,
        state: str | None = None,
    ) -> None:
        if self._diagnostics is not None:
            self._diagnostics.emit(
                Diagnostic(
                    code=code,
                    level=level,
                    message=message,
                    duration_ms=duration_ms,
                    request_id=request_id,
                    statement_id=statement_id,
                    state=state,
                )
            )
