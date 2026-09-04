from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from math import isfinite
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from semantic_api.models import CompilationMode

_RUN_ID = re.compile(r"^run_[0-9a-f]{32}$")
_SAFE_METADATA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_DECIMAL = Decimal("1e28")


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        extra="forbid",
        allow_inf_nan=False,
    )


class RunState(StrEnum):
    QUEUED = "Queued"
    STARTING = "Starting"
    RUNNING = "Running"
    CANCELLED = "Cancelled"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"

    @property
    def terminal(self) -> bool:
        return self in {self.CANCELLED, self.SUCCEEDED, self.FAILED}


class ExecutionMode(StrEnum):
    THREAD = "thread"


class OutputMode(StrEnum):
    NORMAL = "normal"
    STREAM = "stream"


class StartRunRequest(ApiModel):
    run_id: str
    client_request_id: str
    workload: str
    question: str = Field(min_length=1, max_length=4_000)
    evaluation_clock: datetime
    evaluation_timezone: str = Field(min_length=1, max_length=100)
    compilation_mode: CompilationMode
    execution_mode: ExecutionMode
    output_mode: OutputMode
    requested_by: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(min_length=1, max_length=128)

    @field_validator("run_id")
    @classmethod
    def canonical_run_id(cls, value: str) -> str:
        if not _RUN_ID.fullmatch(value):
            raise ValueError("run_id must use the canonical run_<32 lowercase hex> form")
        return value

    @field_validator("client_request_id", "workload")
    @classmethod
    def safe_metadata(cls, value: str) -> str:
        if not _SAFE_METADATA.fullmatch(value):
            raise ValueError("value must contain 1-64 safe ASCII characters")
        return value

    @field_validator("question", "requested_by", "trace_id")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or any(
            unicodedata.category(character).startswith("C") for character in value
        ):
            raise ValueError("value cannot be blank or contain control characters")
        return value

    @field_validator("evaluation_clock")
    @classmethod
    def aware_clock(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluation_clock must include an explicit UTC offset")
        return value

    @field_validator("evaluation_timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        if not value.strip() or (value != "UTC" and "/" not in value):
            raise ValueError("evaluation_timezone must be an IANA time zone name")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("evaluation_timezone must be an IANA time zone name") from exc
        return value


class TokenUsage(ApiModel):
    input_tokens: StrictInt = Field(default=0, ge=0)
    output_tokens: StrictInt = Field(default=0, ge=0)


class DiagnosticSummary(ApiModel):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=512)
    stage: str | None = Field(default=None, max_length=64)
    occurred_at: datetime


class NodeSummary(ApiModel):
    node_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=64)
    state: RunState
    started_at: datetime | None = None
    completed_at: datetime | None = None


class StageSummary(ApiModel):
    stage_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    state: RunState
    started_at: datetime | None = None
    completed_at: datetime | None = None
    nodes: list[NodeSummary] = Field(default_factory=list, max_length=1_000)


class RunStatus(ApiModel):
    run_id: str
    state: RunState
    started_at: datetime | None = None
    finalized_at: datetime | None = None
    stages: list[StageSummary] = Field(default_factory=list, max_length=100)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    diagnostics: list[DiagnosticSummary] = Field(default_factory=list, max_length=100)


class OperatorKind(StrEnum):
    SOURCE = "SOURCE"
    SELECT = "SELECT"
    FILTER = "FILTER"
    AGGREGATE = "AGGREGATE"
    PIVOT = "PIVOT"
    DERIVE = "DERIVE"
    PROJECT = "PROJECT"
    SORT = "SORT"
    LIMIT = "LIMIT"
    JOIN = "JOIN"


class ScalarType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"


class ColumnFormat(StrEnum):
    TEXT = "text"
    CURRENCY = "currency"
    PERCENT = "percent"
    NUMBER = "number"
    DATE = "date"
    TIMESTAMP = "timestamp"


class ResultStorage(StrEnum):
    INLINE = "inline"
    PARQUET = "parquet"


class LineageNodeKind(StrEnum):
    LOGICAL = "logical"
    PHYSICAL = "physical"
    SOURCE = "source"
    RESULT = "result"


class LineageRelation(StrEnum):
    REALIZED_AS = "realized_as"
    READS_FROM = "reads_from"
    DEPENDS_ON = "depends_on"
    PRODUCES = "produces"


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DiagnosticScope(StrEnum):
    RUN = "run"
    STAGE = "stage"
    NODE = "node"


class SqgFilter(ApiModel):
    field: str = Field(min_length=1, max_length=128)
    operator: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=512)


class SqgSummary(ApiModel):
    version: str = Field(min_length=1, max_length=128)
    intent: str = Field(min_length=1, max_length=512)
    ontology: str = Field(min_length=1, max_length=128)
    resolved_members: list[str] = Field(default_factory=list, max_length=100)
    metrics: list[str] = Field(default_factory=list, max_length=100)
    dimensions: list[str] = Field(default_factory=list, max_length=100)
    filters: list[SqgFilter] = Field(default_factory=list, max_length=100)
    policy_checks: list[str] = Field(default_factory=list, max_length=100)


class PhysicalNodeDetail(ApiModel):
    id: str = Field(min_length=1, max_length=128)
    kind: OperatorKind
    label: str = Field(min_length=1, max_length=256)
    plain_language: str = Field(min_length=1, max_length=1_000)
    inputs: list[str] = Field(default_factory=list, max_length=100)
    output_fields: list[str] = Field(default_factory=list, max_length=100)


JsonScalar = str | int | float | bool | None


class ResultColumn(ApiModel):
    key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=256)
    data_type: ScalarType
    format: ColumnFormat
    nullable: StrictBool


class ResultSet(ApiModel):
    columns: list[ResultColumn] = Field(max_length=100)
    rows: list[list[JsonScalar]] = Field(max_length=1_000)
    row_count: StrictInt = Field(ge=0, le=2_147_483_647)
    truncated: StrictBool

    @model_validator(mode="after")
    def validate_rows(self) -> ResultSet:
        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("result column keys must be unique")
        if self.row_count < len(self.rows):
            raise ValueError("row_count cannot be smaller than the inline rows")
        if self.truncated != (self.row_count > len(self.rows)):
            raise ValueError("truncated must reflect omitted rows")
        for row in self.rows:
            if len(row) != len(self.columns):
                raise ValueError("result rows must match the column count")
            for value, column in zip(row, self.columns, strict=True):
                self._validate_cell(value, column)
        return self

    @staticmethod
    def _validate_cell(value: JsonScalar, column: ResultColumn) -> None:
        if value is None:
            if not column.nullable:
                raise ValueError("non-nullable result columns cannot contain null")
            return
        valid = False
        if column.data_type is ScalarType.STRING:
            valid = isinstance(value, str) and len(value) <= 4_000
        elif column.data_type is ScalarType.INTEGER:
            valid = (
                isinstance(value, int)
                and not isinstance(value, bool)
                and -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER
            )
        elif column.data_type in {ScalarType.FLOAT, ScalarType.DECIMAL}:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    number = Decimal(str(value))
                    valid = (
                        (not isinstance(value, float) or isfinite(value))
                        and number.is_finite()
                        and abs(number) <= _MAX_DECIMAL
                    )
                except InvalidOperation:
                    valid = False
        elif column.data_type is ScalarType.BOOLEAN:
            valid = isinstance(value, bool)
        elif (
            column.data_type is ScalarType.DATE
            and isinstance(value, str)
            and _DATE.fullmatch(value)
        ):
            try:
                date.fromisoformat(value)
                valid = len(value) == 10
            except ValueError:
                valid = False
        elif (
            column.data_type is ScalarType.TIMESTAMP
            and isinstance(value, str)
            and _RFC3339.fullmatch(value)
        ):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                valid = parsed.tzinfo is not None and parsed.utcoffset() is not None
            except ValueError:
                valid = False
        if not valid:
            raise ValueError("result cell does not match its declared scalar type")


class CommittedManifest(ApiModel):
    result_id: str = Field(min_length=1, max_length=128)
    run_id: str
    node_id: str = Field(min_length=1, max_length=128)
    storage: ResultStorage
    uri: str = Field(min_length=1, max_length=2_048)
    row_count: StrictInt = Field(ge=0)
    byte_count: StrictInt = Field(ge=0)
    checksum: str = Field(min_length=1, max_length=256)
    committed_at: datetime


class LineageParameter(ApiModel):
    name: str = Field(min_length=1, max_length=128)
    data_type: ScalarType


class LineageNodeDetail(ApiModel):
    id: str = Field(min_length=1, max_length=128)
    kind: LineageNodeKind
    operation: str | None = Field(default=None, max_length=128)
    source_alias: str | None = Field(default=None, max_length=128)
    source_type: str | None = Field(default=None, max_length=128)
    result_id: str | None = Field(default=None, max_length=128)
    parameters: list[LineageParameter] = Field(default_factory=list, max_length=100)


class LineageEdgeDetail(ApiModel):
    source: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)
    relation: LineageRelation


class LineageDetail(ApiModel):
    version: Literal["query-runtime/v0"] = "query-runtime/v0"
    run_id: str
    nodes: list[LineageNodeDetail] = Field(default_factory=list, max_length=5_000)
    edges: list[LineageEdgeDetail] = Field(default_factory=list, max_length=10_000)


class DetailDiagnostic(ApiModel):
    sequence: StrictInt = Field(ge=0)
    run_id: str
    scope: DiagnosticScope
    scope_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=1_000)
    recovery: str = Field(min_length=1, max_length=1_000)
    severity: DiagnosticSeverity
    occurred_at: datetime


class RunDetail(ApiModel):
    run_id: str
    question: str = Field(min_length=1, max_length=4_000)
    sqg: SqgSummary
    physical_nodes: list[PhysicalNodeDetail] = Field(default_factory=list, max_length=1_000)
    result: ResultSet | None = None
    manifest: CommittedManifest | None = None
    lineage: LineageDetail
    diagnostics: list[DetailDiagnostic] = Field(default_factory=list, max_length=1_000)

    @model_validator(mode="after")
    def validate_detail(self) -> RunDetail:
        if not _RUN_ID.fullmatch(self.run_id):
            raise ValueError("detail run_id is not canonical")
        physical_ids = [node.id for node in self.physical_nodes]
        if len(physical_ids) != len(set(physical_ids)):
            raise ValueError("physical node IDs must be unique")
        if self.manifest is not None and self.manifest.run_id != self.run_id:
            raise ValueError("manifest run_id must match detail run_id")
        if self.lineage.run_id != self.run_id:
            raise ValueError("lineage run_id must match detail run_id")
        lineage_ids = [node.id for node in self.lineage.nodes]
        if len(lineage_ids) != len(set(lineage_ids)):
            raise ValueError("lineage node IDs must be unique")
        known = set(lineage_ids)
        if any(edge.source not in known or edge.target not in known for edge in self.lineage.edges):
            raise ValueError("lineage edges must reference known nodes")
        previous = -1
        for diagnostic in self.diagnostics:
            if diagnostic.run_id != self.run_id or diagnostic.sequence <= previous:
                raise ValueError("diagnostics must match the run and increase by sequence")
            previous = diagnostic.sequence
        return self
