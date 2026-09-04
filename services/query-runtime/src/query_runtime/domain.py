"""Versioned internal contracts for the standalone query runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DomainVersion = Literal["query-runtime/v0"]
DOMAIN_VERSION: DomainVersion = "query-runtime/v0"


def utc_now() -> datetime:
    return datetime.now(UTC)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


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


class PhysicalNodeKind(StrEnum):
    SOURCE_FRAGMENT = "SOURCE_FRAGMENT"
    OPERATOR = "OPERATOR"


class ScalarType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"


class ExpressionKind(StrEnum):
    COLUMN = "column"
    LITERAL = "literal"
    ADD = "add"
    SUBTRACT = "subtract"
    MULTIPLY = "multiply"
    DIVIDE = "divide"
    EQUAL = "equal"
    NOT_EQUAL = "not_equal"
    LESS_THAN = "less_than"
    LESS_EQUAL = "less_equal"
    GREATER_THAN = "greater_than"
    GREATER_EQUAL = "greater_equal"
    AND = "and"
    OR = "or"
    NOT = "not"
    IS_NULL = "is_null"
    COALESCE = "coalesce"


class AggregateFunction(StrEnum):
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    COUNT = "count"


class JoinType(StrEnum):
    INNER = "inner"
    LEFT = "left"
    RIGHT = "right"
    FULL = "full"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class TimeGrain(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class CapabilityCatalog(FrozenModel):
    version: DomainVersion = DOMAIN_VERSION
    source_alias: str
    source_type: str
    operator_kinds: frozenset[OperatorKind]
    joins: bool = False
    aggregate_functions: frozenset[AggregateFunction] = frozenset()
    time_grains: frozenset[TimeGrain] = frozenset()
    spill_supported: bool = False
    max_rows: int | None = Field(default=None, gt=0)
    max_bytes: int | None = Field(default=None, gt=0)

    def supports(self, kind: OperatorKind) -> bool:
        if kind is OperatorKind.JOIN:
            return self.joins and kind in self.operator_kinds
        return kind in self.operator_kinds


class BoundSource(FrozenModel):
    version: DomainVersion = DOMAIN_VERSION
    alias: str
    source_type: str
    object_name: str


class BoundColumn(FrozenModel):
    version: DomainVersion = DOMAIN_VERSION
    concept: str
    source_alias: str
    column_name: str
    data_type: ScalarType


class TypedExpression(FrozenModel):
    kind: ExpressionKind
    data_type: ScalarType
    column: str | None = None
    value: str | int | float | bool | None = None
    args: tuple[TypedExpression, ...] = ()

    @model_validator(mode="after")
    def validate_shape(self) -> TypedExpression:
        if self.kind is ExpressionKind.COLUMN and (not self.column or self.args):
            raise ValueError("column expressions require exactly a column name")
        if self.kind is ExpressionKind.LITERAL and (self.column or self.args):
            raise ValueError("literal expressions cannot contain column or args")
        if self.kind not in {ExpressionKind.COLUMN, ExpressionKind.LITERAL} and self.column:
            raise ValueError("operator expressions cannot contain a column name")
        return self

    @classmethod
    def col(cls, name: str, data_type: ScalarType) -> TypedExpression:
        return cls(kind=ExpressionKind.COLUMN, column=name, data_type=data_type)

    @classmethod
    def literal(
        cls, value: str | int | float | bool | None, data_type: ScalarType
    ) -> TypedExpression:
        return cls(kind=ExpressionKind.LITERAL, value=value, data_type=data_type)


class BoundPredicate(FrozenModel):
    expression: TypedExpression

    @model_validator(mode="after")
    def boolean_expression(self) -> BoundPredicate:
        if self.expression.data_type is not ScalarType.BOOLEAN:
            raise ValueError("predicate expression must be boolean")
        return self


class NamedExpression(FrozenModel):
    name: str
    expression: TypedExpression


class AggregateSpec(FrozenModel):
    name: str
    function: AggregateFunction
    expression: TypedExpression | None = None


class SortSpec(FrozenModel):
    column: str
    direction: SortDirection = SortDirection.ASC
    nulls_first: bool = False


class JoinKey(FrozenModel):
    left: str
    right: str


class OperatorSpec(FrozenModel):
    kind: OperatorKind
    columns: tuple[str, ...] = ()
    predicate: BoundPredicate | None = None
    group_by: tuple[str, ...] = ()
    aggregates: tuple[AggregateSpec, ...] = ()
    expressions: tuple[NamedExpression, ...] = ()
    sort: tuple[SortSpec, ...] = ()
    limit: int | None = Field(default=None, ge=0)
    join_type: JoinType | None = None
    join_keys: tuple[JoinKey, ...] = ()
    pivot_index: tuple[str, ...] = ()
    pivot_column: str | None = None
    pivot_value: str | None = None
    pivot_values: tuple[str, ...] = ()
    time_grain: TimeGrain | None = None


class BoundParameter(FrozenModel):
    name: str
    data_type: ScalarType
    value: str | int | float | bool | None = Field(exclude=True)

    def safe_metadata(self) -> dict[str, str]:
        return {"name": self.name, "data_type": self.data_type.value}


class SourceFragment(FrozenModel):
    source: BoundSource
    operations: tuple[OperatorSpec, ...]
    parameters: tuple[BoundParameter, ...] = ()
    bound_columns: tuple[BoundColumn, ...] = ()


class LogicalOperationRef(FrozenModel):
    logical_node_id: str
    operation: OperatorKind


class PhysicalNode(FrozenModel):
    id: str
    kind: PhysicalNodeKind
    operation: OperatorKind
    dependencies: tuple[str, ...] = ()
    wave: int = Field(ge=0)
    logical_node_ids: tuple[str, ...]
    logical_operations: tuple[LogicalOperationRef, ...] = ()
    source_fragment: SourceFragment | None = None
    operator: OperatorSpec | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> PhysicalNode:
        if len(self.logical_node_ids) > 1 and not self.logical_operations:
            raise ValueError("fused nodes require logical operation metadata")
        if self.logical_operations and tuple(
            item.logical_node_id for item in self.logical_operations
        ) != self.logical_node_ids:
            raise ValueError("logical operation metadata must match logical node IDs")
        if self.kind is PhysicalNodeKind.SOURCE_FRAGMENT:
            if self.source_fragment is None or self.operator is not None:
                raise ValueError("source fragment node requires only source_fragment")
            if not self.source_fragment.operations:
                raise ValueError("source fragment requires at least one operation")
            if self.operation is not self.source_fragment.operations[-1].kind:
                raise ValueError("node operation must match the final fragment operation")
        elif self.operator is None or self.source_fragment is not None:
            raise ValueError("operator node requires only operator")
        elif self.operation is not self.operator.kind:
            raise ValueError("node operation must match the local operator payload")
        return self


class PhysicalPlan(FrozenModel):
    version: DomainVersion = DOMAIN_VERSION
    id: str
    nodes: tuple[PhysicalNode, ...]
    output_node_id: str


class ExecutionState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    SKIPPED = "SKIPPED"


TERMINAL_STATES = frozenset(
    {
        ExecutionState.SUCCEEDED,
        ExecutionState.FAILED,
        ExecutionState.CANCELLED,
        ExecutionState.TIMED_OUT,
        ExecutionState.SKIPPED,
    }
)

STATE_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.PENDING: frozenset(
        {ExecutionState.READY, ExecutionState.CANCELLED, ExecutionState.SKIPPED}
    ),
    ExecutionState.READY: frozenset(
        {
            ExecutionState.RUNNING,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
            ExecutionState.SKIPPED,
        }
    ),
    ExecutionState.RUNNING: frozenset(
        {
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
            ExecutionState.TIMED_OUT,
        }
    ),
    **{state: frozenset() for state in TERMINAL_STATES},
}


class SchemaField(FrozenModel):
    name: str
    data_type: str
    nullable: bool = True


class TableSchema(FrozenModel):
    fields: tuple[SchemaField, ...]


class RowBatch(FrozenModel):
    schema_: TableSchema = Field(alias="schema")
    rows: tuple[tuple[Any, ...], ...]


class ResultHandle(FrozenModel):
    result_id: str
    run_id: str
    node_id: str
    storage: Literal["inline", "parquet"]
    uri: str


class ResultPart(FrozenModel):
    path: str
    rows: int = Field(ge=0)
    bytes: int = Field(ge=0)


class CommittedManifest(FrozenModel):
    version: DomainVersion = DOMAIN_VERSION
    result: ResultHandle
    schema_: TableSchema = Field(alias="schema")
    row_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    parts: tuple[ResultPart, ...]
    committed_at: datetime


class ResultSummary(FrozenModel):
    run_id: str
    state: ExecutionState
    row_count: int | None = Field(default=None, ge=0)
    result: ResultHandle | None = None
    diagnostic_code: str | None = None


class LineageNode(FrozenModel):
    id: str
    kind: Literal["logical", "physical", "source", "result"]
    operation: str | None = None
    source_alias: str | None = None
    source_type: str | None = None
    result_id: str | None = None
    parameter_metadata: tuple[dict[str, str], ...] = ()


class LineageEdge(FrozenModel):
    source: str
    target: str
    relation: Literal["realized_as", "reads_from", "depends_on", "produces"]


class LineageGraph(FrozenModel):
    version: DomainVersion = DOMAIN_VERSION
    run_id: str
    nodes: tuple[LineageNode, ...]
    edges: tuple[LineageEdge, ...]


class DiagnosticEvent(FrozenModel):
    sequence: int = Field(ge=0)
    run_id: str
    scope: Literal["run", "stage", "node"]
    scope_id: str
    state: ExecutionState
    code: str
    message: str
    timestamp: datetime = Field(default_factory=utc_now)
    duration_ms: int | None = Field(default=None, ge=0)
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)
