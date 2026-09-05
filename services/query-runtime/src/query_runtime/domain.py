"""Versioned internal contracts for the standalone query runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from query_runtime.scalar_values import scalar_value

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
    ACT = "ACT"
    ASK = "ASK"
    DATE = "DATE"
    DEDUPLICATE = "DEDUPLICATE"
    DISTINCT = "DISTINCT"
    EXCEPT = "EXCEPT"
    EXPLODE = "EXPLODE"
    IMPUTE = "IMPUTE"
    INTERSECT = "INTERSECT"
    PICK = "PICK"
    RESAMPLE = "RESAMPLE"
    SAMPLE = "SAMPLE"
    SEARCH = "SEARCH"
    SUMMARIZE = "SUMMARIZE"
    UNION_ALL = "UNION_ALL"
    UNION_DISTINCT = "UNION_DISTINCT"
    UNPIVOT = "UNPIVOT"
    WINDOW = "WINDOW"


V0_OPERATOR_KINDS = frozenset(
    {
        OperatorKind.SOURCE,
        OperatorKind.SELECT,
        OperatorKind.FILTER,
        OperatorKind.AGGREGATE,
        OperatorKind.PIVOT,
        OperatorKind.DERIVE,
        OperatorKind.PROJECT,
        OperatorKind.SORT,
        OperatorKind.LIMIT,
        OperatorKind.JOIN,
    }
)
BLOCKED_OPERATOR_KINDS = frozenset({OperatorKind.ACT, OperatorKind.ASK, OperatorKind.SEARCH})
PlanVersion = Literal["query-runtime/v0", "query-runtime/v1"]


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

    @model_validator(mode="after")
    def legacy_capabilities(self) -> CapabilityCatalog:
        if not self.operator_kinds <= V0_OPERATOR_KINDS:
            raise ValueError("v0 source capabilities cannot advertise v1 operators")
        return self

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
    contract_version: ClassVar[str] = "query-runtime/v0"
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

    @model_validator(mode="after")
    def legacy_operator(self) -> OperatorSpec:
        if self.contract_version == "query-runtime/v0" and self.kind not in V0_OPERATOR_KINDS:
            raise ValueError("operator requires query-runtime/v1")
        return self


class WindowSpec(FrozenModel):
    function: Literal["row_number", "rank", "dense_rank", "sum", "min", "max", "count"]
    output: str = Field(min_length=1, max_length=128)
    column: str | None = None
    preceding: int = Field(default=0, strict=True, ge=0, le=100_000)

    @model_validator(mode="after")
    def window_shape(self) -> WindowSpec:
        ranking = self.function in {"row_number", "rank", "dense_rank"}
        if ranking and (self.column is not None or self.preceding != 0):
            raise ValueError("ranking windows do not take a column or frame")
        if not ranking and not self.column:
            raise ValueError("aggregate windows require a column")
        return self


class DateSpec(FrozenModel):
    column: str = Field(min_length=1, max_length=128)
    output: str = Field(min_length=1, max_length=128)
    grain: TimeGrain


class UnpivotSpec(FrozenModel):
    columns: tuple[str, ...] = Field(min_length=1, max_length=128)
    name_column: str = Field(min_length=1, max_length=128)
    value_column: str = Field(min_length=1, max_length=128)


class ExplodeSpec(FrozenModel):
    column: str = Field(min_length=1, max_length=128)
    output: str = Field(min_length=1, max_length=128)


class ImputeSpec(FrozenModel):
    column: str = Field(min_length=1, max_length=128)
    value: TypedExpression

    @model_validator(mode="after")
    def literal_only(self) -> ImputeSpec:
        if self.value.kind is not ExpressionKind.LITERAL or self.value.value is None:
            raise ValueError("imputation requires a non-null typed literal")
        scalar_value(self.value.data_type.value, self.value.value)
        return self


class OperatorSpecV1(OperatorSpec):
    """Opt-in extension; a version is mandatory, never inferred from extra fields."""

    contract_version: ClassVar[str] = "query-runtime/v1"
    version: Literal["query-runtime/v1"]
    kind: OperatorKind
    limit: int | None = Field(default=None, strict=True, ge=0, le=1_000_000)
    partition_by: tuple[str, ...] = Field(default=(), max_length=128)
    window: WindowSpec | None = None
    date: DateSpec | None = None
    unpivot: UnpivotSpec | None = None
    explode: ExplodeSpec | None = None
    impute: ImputeSpec | None = None
    sample_seed: int | None = Field(default=None, strict=True, ge=0, le=2**32 - 1)

    @model_validator(mode="after")
    def extension_shape(self) -> OperatorSpecV1:
        allowed = {
            OperatorKind.SOURCE: set(),
            OperatorKind.SELECT: {"columns", "expressions"},
            OperatorKind.PROJECT: {"columns", "expressions"},
            OperatorKind.DERIVE: {"expressions"},
            OperatorKind.FILTER: {"predicate"},
            OperatorKind.SORT: {"sort"},
            OperatorKind.LIMIT: {"limit"},
            OperatorKind.AGGREGATE: {"group_by", "aggregates"},
            OperatorKind.SUMMARIZE: {"group_by", "aggregates"},
            OperatorKind.JOIN: {"join_type", "join_keys"},
            OperatorKind.PIVOT: {"pivot_index", "pivot_column", "pivot_value", "pivot_values"},
            OperatorKind.DISTINCT: {"columns"},
            OperatorKind.DEDUPLICATE: {"partition_by", "sort"},
            OperatorKind.PICK: {"partition_by", "sort", "limit"},
            OperatorKind.SAMPLE: {"sample_seed", "limit"},
            OperatorKind.WINDOW: {"partition_by", "sort", "window"},
            OperatorKind.DATE: {"date"},
            OperatorKind.RESAMPLE: {"date", "group_by", "aggregates"},
            OperatorKind.UNPIVOT: {"unpivot"},
            OperatorKind.EXPLODE: {"explode"},
            OperatorKind.IMPUTE: {"impute"},
        }.get(self.kind, set())
        if set(self.model_dump(exclude_defaults=True)) - allowed - {"kind", "version"}:
            raise ValueError("operator contains parameters that its form does not support")
        required = {
            OperatorKind.FILTER: self.predicate is not None,
            OperatorKind.LIMIT: self.limit is not None,
            OperatorKind.SELECT: bool(self.columns or self.expressions),
            OperatorKind.PROJECT: bool(self.columns or self.expressions),
            OperatorKind.DERIVE: bool(self.expressions),
            OperatorKind.AGGREGATE: bool(self.aggregates),
            OperatorKind.SUMMARIZE: bool(self.aggregates),
            OperatorKind.RESAMPLE: bool(self.aggregates),
            OperatorKind.JOIN: self.join_type is not None and bool(self.join_keys),
            OperatorKind.SORT: bool(self.sort),
            OperatorKind.PIVOT: bool(self.pivot_column and self.pivot_value and self.pivot_values),
        }
        if not required.get(self.kind, True):
            raise ValueError(f"{self.kind} requires its supported form payload")
        for aggregate in self.aggregates:
            if aggregate.function is not AggregateFunction.COUNT and aggregate.expression is None:
                raise ValueError("non-COUNT aggregates require an expression")
        identifiers = [
            *self.columns,
            *self.group_by,
            *self.partition_by,
            *self.pivot_index,
            *self.pivot_values,
            *(item.name for item in self.expressions),
            *(item.name for item in self.aggregates),
            *(item.column for item in self.sort),
            *(key.left for key in self.join_keys),
            *(key.right for key in self.join_keys),
        ]
        identifiers.extend(
            name for name in (self.pivot_column, self.pivot_value) if name is not None
        )
        if self.window is not None:
            identifiers.append(self.window.output)
            if self.window.column is not None:
                identifiers.append(self.window.column)
        if self.date is not None:
            identifiers.extend((self.date.column, self.date.output))
        if self.unpivot is not None:
            identifiers.extend(
                (
                    *self.unpivot.columns,
                    self.unpivot.name_column,
                    self.unpivot.value_column,
                )
            )
        if self.explode is not None:
            identifiers.extend((self.explode.column, self.explode.output))
        if self.impute is not None:
            identifiers.append(self.impute.column)
        if any(not name or "\x00" in name for name in identifiers):
            raise ValueError("operator identifiers cannot be empty or contain null bytes")
        expressions = [item.expression for item in self.expressions]
        expressions.extend(
            item.expression for item in self.aggregates if item.expression is not None
        )
        if self.predicate is not None:
            expressions.append(self.predicate.expression)
        if self.impute is not None:
            expressions.append(self.impute.value)
        for expression in expressions:
            validate_expression_v1(expression)
        for specs in (self.sort, self.aggregates, self.expressions, self.join_keys):
            if len(specs) > 128:
                raise ValueError("operator lists are limited to 128 entries")
        payloads = {
            "window": {OperatorKind.WINDOW},
            "date": {OperatorKind.DATE, OperatorKind.RESAMPLE},
            "unpivot": {OperatorKind.UNPIVOT},
            "explode": {OperatorKind.EXPLODE},
            "impute": {OperatorKind.IMPUTE},
            "sample_seed": {OperatorKind.SAMPLE},
        }
        for field, kinds in payloads.items():
            present = getattr(self, field) is not None
            if present != (self.kind in kinds):
                raise ValueError(f"{field} is required only for {sorted(kinds)}")
        if self.kind in {OperatorKind.PICK, OperatorKind.DEDUPLICATE, OperatorKind.WINDOW}:
            if not self.sort:
                raise ValueError("ordered selection/window requires explicit sort keys")
        elif self.partition_by:
            raise ValueError("partition_by requires PICK, DEDUPLICATE or WINDOW")
        if self.kind is OperatorKind.SAMPLE and self.limit is None:
            raise ValueError("SAMPLE requires a limit")
        if self.kind is OperatorKind.RESAMPLE and not self.aggregates:
            raise ValueError("RESAMPLE requires aggregates")
        if self.kind in {OperatorKind.DISTINCT, OperatorKind.DEDUPLICATE} and self.limit:
            raise ValueError("deduplication does not take a limit")
        for values in (self.partition_by, self.columns, self.group_by, self.pivot_values):
            if len(values) > 128 or len(set(values)) != len(values):
                raise ValueError("column/value lists must be unique and at most 128 items")
        return self


def validate_expression_v1(expression: TypedExpression) -> None:
    pending = [(expression, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > 16 or count > 128:
            raise ValueError("expression exceeds depth/node limits")
        if node.kind is ExpressionKind.LITERAL:
            if node.args or node.column:
                raise ValueError("literal expression cannot have arguments or a column")
            scalar_value(node.data_type.value, node.value)
        elif node.kind is ExpressionKind.COLUMN:
            if not node.column or "\x00" in node.column or node.args:
                raise ValueError("column expression requires a valid column only")
        else:
            if node.column:
                raise ValueError("operator expression cannot have a column")
            if node.kind is ExpressionKind.COALESCE:
                if not node.args or any(arg.data_type is not node.data_type for arg in node.args):
                    raise ValueError("COALESCE requires arguments with matching declared types")
            else:
                arity = 1 if node.kind in {ExpressionKind.NOT, ExpressionKind.IS_NULL} else 2
                if len(node.args) != arity:
                    raise ValueError("expression has invalid arity")
        pending.extend((arg, depth + 1) for arg in node.args)


def validate_operator_v1(operation: OperatorSpec) -> None:
    """Revalidate copied/constructed models at the planner and execution boundaries."""
    payload = operation.model_dump()
    payload.setdefault("version", "query-runtime/v1")
    OperatorSpecV1.model_validate(payload)


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

    @model_validator(mode="after")
    def legacy_fragment(self) -> SourceFragment:
        if any(isinstance(item, OperatorSpecV1) for item in self.operations):
            raise ValueError("v0 source fragments cannot contain v1 payloads")
        return self


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
    operator: OperatorSpecV1 | OperatorSpec | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> PhysicalNode:
        if len(self.logical_node_ids) > 1 and not self.logical_operations:
            raise ValueError("fused nodes require logical operation metadata")
        if (
            self.logical_operations
            and tuple(item.logical_node_id for item in self.logical_operations)
            != self.logical_node_ids
        ):
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
    version: PlanVersion = DOMAIN_VERSION
    id: str
    nodes: tuple[PhysicalNode, ...]
    output_node_id: str

    @model_validator(mode="after")
    def version_boundary(self) -> PhysicalPlan:
        if self.version == DOMAIN_VERSION and any(
            isinstance(node.operator, OperatorSpecV1)
            or node.operation not in V0_OPERATOR_KINDS
            or any(ref.operation not in V0_OPERATOR_KINDS for ref in node.logical_operations)
            for node in self.nodes
        ):
            raise ValueError("v1 operators require a query-runtime/v1 plan")
        if self.version == "query-runtime/v1":
            for node in self.nodes:
                if node.operator is not None:
                    validate_operator_v1(node.operator)
                if node.source_fragment is not None:
                    for operation in node.source_fragment.operations:
                        validate_operator_v1(operation)
        return self


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
