from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from .exceptions import ConfigurationError

type CellValue = str | None
type ParameterValue = str | int | float | bool | Decimal | date | datetime | None


class FetchDisposition(StrEnum):
    INLINE = "INLINE"
    EXTERNAL_LINKS = "EXTERNAL_LINKS"


class ResultFormat(StrEnum):
    JSON_ARRAY = "JSON_ARRAY"
    ARROW_STREAM = "ARROW_STREAM"
    CSV = "CSV"


class WaitTimeoutAction(StrEnum):
    CONTINUE = "CONTINUE"
    CANCEL = "CANCEL"


class StatementState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    CLOSED = "CLOSED"


class ParameterType(StrEnum):
    BOOLEAN = "BOOLEAN"
    BYTE = "BYTE"
    SHORT = "SHORT"
    INT = "INT"
    LONG = "LONG"
    FLOAT = "FLOAT"
    DOUBLE = "DOUBLE"
    STRING = "STRING"
    DATE = "DATE"
    TIMESTAMP = "TIMESTAMP"


@dataclass(frozen=True)
class DecimalType:
    precision: int
    scale: int

    def __post_init__(self) -> None:
        if not 1 <= self.precision <= 38:
            raise ConfigurationError("decimal precision must be between 1 and 38")
        if not 0 <= self.scale <= self.precision:
            raise ConfigurationError("decimal scale must be between 0 and precision")

    @property
    def api_name(self) -> str:
        return f"DECIMAL({self.precision},{self.scale})"


def _parameter_value(value: ParameterValue) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


@dataclass(frozen=True)
class StatementParameter:
    name: str
    type: ParameterType | DecimalType
    value: ParameterValue

    def __post_init__(self) -> None:
        if not self.name.isidentifier() or not self.name.isascii():
            raise ConfigurationError("parameter names must be ASCII identifiers")

    @classmethod
    def string(cls, name: str, value: str | None) -> StatementParameter:
        return cls(name=name, type=ParameterType.STRING, value=value)

    @classmethod
    def decimal(
        cls,
        name: str,
        value: Decimal | None,
        *,
        precision: int,
        scale: int,
    ) -> StatementParameter:
        return cls(
            name=name,
            type=DecimalType(precision=precision, scale=scale),
            value=value,
        )

    def to_api_dict(self) -> dict[str, str]:
        type_name = self.type.value if isinstance(self.type, ParameterType) else self.type.api_name
        result = {"name": self.name, "type": type_name}
        value = _parameter_value(self.value)
        if value is not None:
            result["value"] = value
        return result


@dataclass(frozen=True)
class ServiceError:
    error_code: str | None = None


@dataclass(frozen=True)
class StatementStatus:
    state: StatementState
    error: ServiceError | None = None
    sql_state: str | None = None


@dataclass(frozen=True)
class ResultColumn:
    name: str
    position: int
    type_name: str
    type_text: str


@dataclass(frozen=True)
class ResultChunk:
    chunk_index: int | None = None
    row_count: int | None = None
    row_offset: int | None = None
    byte_count: int | None = None
    rows: tuple[tuple[CellValue, ...], ...] = ()
    payloads: tuple[bytes, ...] = ()
    next_chunk_index: int | None = None
    next_chunk_internal_link: str | None = None


@dataclass(frozen=True)
class ResultManifest:
    columns: tuple[ResultColumn, ...]
    result_format: ResultFormat
    total_row_count: int | None
    total_byte_count: int | None
    total_chunk_count: int | None
    truncated: bool


@dataclass(frozen=True)
class StatementResponse:
    statement_id: str
    status: StatementStatus
    manifest: ResultManifest | None = None
    result: dict[str, object] | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class StatementExecutionResult:
    statement_id: str
    columns: tuple[ResultColumn, ...]
    rows: tuple[tuple[CellValue, ...], ...]
    payloads: tuple[bytes, ...]
    result_format: ResultFormat
    elapsed_ms: int
    request_ids: tuple[str, ...]


@dataclass(frozen=True)
class PhysicalSourceFragment:
    source_name: str
    sql: str
    parameters: tuple[StatementParameter, ...] = ()


@dataclass(frozen=True)
class LineageMetadata:
    resolver: str
    source_name: str
    statement_id: str
    physical_fragment_sha256: str


@dataclass(frozen=True)
class TabularResult:
    columns: tuple[ResultColumn, ...]
    rows: tuple[tuple[CellValue, ...], ...]
    lineage: LineageMetadata
    elapsed_ms: int


class ResolverOperation(StrEnum):
    SELECT = "SELECT"
    FILTER = "FILTER"
    AGGREGATE = "AGGREGATE"
    JOIN = "JOIN"
    SORT = "SORT"
    LIMIT = "LIMIT"


@dataclass(frozen=True)
class CapabilityDeclaration:
    operations: tuple[ResolverOperation, ...]
    aggregates: tuple[str, ...]
    result_types: tuple[str, ...]
    named_parameterization: bool
    asynchronous_execution: bool
    cancellation: bool
    result_chunks: bool
    external_links: bool
    disposition_formats: dict[str, tuple[str, ...]]

    @classmethod
    def databricks_sql(cls) -> CapabilityDeclaration:
        return cls(
            operations=tuple(ResolverOperation),
            aggregates=("AVG", "COUNT", "MAX", "MIN", "SUM"),
            result_types=(
                "ARRAY",
                "BINARY",
                "BOOLEAN",
                "BYTE",
                "CHAR",
                "DATE",
                "DECIMAL",
                "DOUBLE",
                "FLOAT",
                "INT",
                "INTERVAL",
                "LONG",
                "MAP",
                "NULL",
                "SHORT",
                "STRING",
                "STRUCT",
                "TIMESTAMP",
                "USER_DEFINED_TYPE",
            ),
            named_parameterization=True,
            asynchronous_execution=True,
            cancellation=True,
            result_chunks=True,
            external_links=True,
            disposition_formats={
                FetchDisposition.INLINE.value: (ResultFormat.JSON_ARRAY.value,),
                FetchDisposition.EXTERNAL_LINKS.value: (ResultFormat.JSON_ARRAY.value,),
            },
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "aggregates": list(self.aggregates),
            "asynchronous_execution": self.asynchronous_execution,
            "cancellation": self.cancellation,
            "disposition_formats": {
                key: list(values) for key, values in sorted(self.disposition_formats.items())
            },
            "external_links": self.external_links,
            "named_parameterization": self.named_parameterization,
            "operations": [operation.value for operation in self.operations],
            "result_chunks": self.result_chunks,
            "result_types": list(self.result_types),
        }
