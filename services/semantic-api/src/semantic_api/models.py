from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DiagnosticStage(StrEnum):
    INITIALIZE = "initialize"
    PROVIDER = "provider"
    VALIDATE = "validate"
    REPAIR = "repair"
    API = "api"


class Diagnostic(StrictModel):
    code: str
    severity: DiagnosticSeverity
    stage: DiagnosticStage
    message: str
    path: str | None = None
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class MemberResolutionMode(StrEnum):
    EXACT = "exact"
    EXACT_AND_SYNONYM = "exact_and_synonym"


class CompilationMode(StrEnum):
    REGIONAL_QUARTERLY_PROFIT = "regional_quarterly_profit"
    MONTHLY_REGIONAL_COMPARISON = "monthly_regional_comparison"


class ProviderSelection(StrEnum):
    STATIC = "static"


class CompileStatus(StrEnum):
    SUCCEEDED = "succeeded"
    CLARIFICATION_REQUIRED = "clarification_required"
    FAILED = "failed"


class CompileRequest(StrictModel):
    api_version: Literal["v1"] = "v1"
    question: str = Field(min_length=1, max_length=4_000)
    evaluation_clock: datetime
    evaluation_timezone: str = Field(min_length=1, max_length=100)
    ontology_scope: list[str] = Field(default_factory=list, max_length=100)
    member_resolution_mode: MemberResolutionMode = MemberResolutionMode.EXACT_AND_SYNONYM
    compilation_mode: CompilationMode = CompilationMode.REGIONAL_QUARTERLY_PROFIT
    provider_selection: ProviderSelection = ProviderSelection.STATIC
    correlation_id: str | None = Field(default=None, max_length=128)

    @field_validator("evaluation_clock")
    @classmethod
    def require_aware_clock(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluation_clock must include an explicit UTC offset")
        return value


class InitializeRequest(CompileRequest):
    pass


class ResolvedTermKind(StrEnum):
    ENTITY = "entity"
    FIELD = "field"
    METRIC = "metric"
    MEMBER = "member"
    TIME_WINDOW = "time_window"


class ResolutionSource(StrEnum):
    MACHINE_ID = "machine_id"
    LABEL = "label"
    SYNONYM = "synonym"
    RELATIVE_TIME = "relative_time"


class ResolvedTerm(StrictModel):
    source_text: str
    kind: ResolvedTermKind
    machine_id: str
    resolution_source: ResolutionSource


class TimeWindow(StrictModel):
    source_text: str
    start: datetime
    end_exclusive: datetime
    timezone: str
    grain: Literal["year", "quarter", "month"]


class QueryPolicy(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class OntologyMember(StrictModel):
    id: str
    label: str
    synonyms: list[str] = Field(default_factory=list)
    enabled: bool = True


class OntologyEntity(StrictModel):
    id: str
    label: str
    synonyms: list[str] = Field(default_factory=list)
    enabled: bool = True
    query_policy: QueryPolicy = QueryPolicy.ALLOW


class ScalarType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATETIME = "datetime"


class OntologyField(StrictModel):
    id: str
    entity_id: str
    label: str
    data_type: ScalarType
    synonyms: list[str] = Field(default_factory=list)
    members: list[OntologyMember] = Field(default_factory=list)
    enabled: bool = True
    query_policy: QueryPolicy = QueryPolicy.ALLOW


class OntologyMetric(StrictModel):
    id: str
    entity_id: str
    label: str
    data_type: ScalarType = ScalarType.NUMBER
    synonyms: list[str] = Field(default_factory=list)
    enabled: bool = True
    query_policy: QueryPolicy = QueryPolicy.ALLOW


class OntologyRelation(StrictModel):
    id: str
    from_entity_id: str
    to_entity_id: str
    from_field_id: str
    to_field_id: str
    label: str
    enabled: bool = True
    query_policy: QueryPolicy = QueryPolicy.ALLOW


class OntologyDocument(StrictModel):
    schema_version: Literal["ontology.v0"]
    ontology_id: str
    version: str
    entities: list[OntologyEntity]
    fields: list[OntologyField]
    metrics: list[OntologyMetric]
    relations: list[OntologyRelation] = Field(default_factory=list)


class SemanticContext(StrictModel):
    ontology_id: str
    ontology_version: str
    entities: list[OntologyEntity]
    fields: list[OntologyField]
    metrics: list[OntologyMetric]
    relations: list[OntologyRelation]


class InitializeResponse(StrictModel):
    api_version: Literal["v1"] = "v1"
    status: CompileStatus
    correlation_id: str
    resolved_terms: list[ResolvedTerm]
    time_windows: list[TimeWindow]
    selected_semantic_context: SemanticContext
    diagnostics: list[Diagnostic]


class ExpressionKind(StrEnum):
    COLUMN = "column"
    LITERAL = "literal"
    BINARY = "binary"


class ColumnExpression(StrictModel):
    kind: Literal[ExpressionKind.COLUMN] = ExpressionKind.COLUMN
    column: str


class LiteralExpression(StrictModel):
    kind: Literal[ExpressionKind.LITERAL] = ExpressionKind.LITERAL
    value: str | int | float | bool | None
    data_type: ScalarType


class BinaryOperator(StrEnum):
    ADD = "add"
    SUBTRACT = "subtract"
    MULTIPLY = "multiply"
    DIVIDE = "divide"


class BinaryExpression(StrictModel):
    kind: Literal[ExpressionKind.BINARY] = ExpressionKind.BINARY
    operator: BinaryOperator
    left: ColumnExpression | LiteralExpression
    right: ColumnExpression | LiteralExpression


Expression = Annotated[
    ColumnExpression | LiteralExpression | BinaryExpression,
    Field(discriminator="kind"),
]


class PredicateOperator(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    BETWEEN = "between"


class Predicate(StrictModel):
    column: str
    operator: PredicateOperator
    value: str | int | float | bool | list[str | int | float | bool] | dict[str, str] | None


class Operator(StrEnum):
    SELECT = "SELECT"
    FILTER = "FILTER"
    AGGREGATE = "AGGREGATE"
    PIVOT = "PIVOT"
    DERIVE = "DERIVE"
    PROJECT = "PROJECT"
    SORT = "SORT"
    JOIN = "JOIN"


class SelectParameters(StrictModel):
    kind: Literal[Operator.SELECT] = Operator.SELECT
    entity_id: str
    columns: list[str]


class FilterParameters(StrictModel):
    kind: Literal[Operator.FILTER] = Operator.FILTER
    predicate: Predicate


class AggregateFunction(StrEnum):
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    COUNT = "count"


class AggregateMeasure(StrictModel):
    source: str
    output: str
    function: AggregateFunction


class AggregateParameters(StrictModel):
    kind: Literal[Operator.AGGREGATE] = Operator.AGGREGATE
    group_by: list[str]
    measures: list[AggregateMeasure]


class PivotParameters(StrictModel):
    kind: Literal[Operator.PIVOT] = Operator.PIVOT
    index: list[str]
    column: str
    value: str
    values: list[str] = Field(min_length=1, max_length=100)


class DerivedColumn(StrictModel):
    output: str
    expression: Expression
    data_type: ScalarType


class DeriveParameters(StrictModel):
    kind: Literal[Operator.DERIVE] = Operator.DERIVE
    columns: list[DerivedColumn]


class Projection(StrictModel):
    source: str
    alias: str


class ProjectParameters(StrictModel):
    kind: Literal[Operator.PROJECT] = Operator.PROJECT
    columns: list[Projection]


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class SortKey(StrictModel):
    column: str
    direction: SortDirection = SortDirection.ASC


class SortParameters(StrictModel):
    kind: Literal[Operator.SORT] = Operator.SORT
    keys: list[SortKey]


class JoinType(StrEnum):
    INNER = "inner"
    LEFT = "left"


class JoinParameters(StrictModel):
    kind: Literal[Operator.JOIN] = Operator.JOIN
    relation_id: str
    left_key: str
    right_key: str
    join_type: JoinType = JoinType.INNER


NodeParameters = Annotated[
    SelectParameters
    | FilterParameters
    | AggregateParameters
    | PivotParameters
    | DeriveParameters
    | ProjectParameters
    | SortParameters
    | JoinParameters,
    Field(discriminator="kind"),
]


class SQGNode(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    name: str = Field(min_length=1, max_length=200)
    operator: Operator
    dependencies: list[str] = Field(default_factory=list, max_length=10)
    parameters: NodeParameters


class ResultColumn(StrictModel):
    name: str
    data_type: ScalarType


class SQG(StrictModel):
    schema_version: Literal["sqg.v0"] = "sqg.v0"
    nodes: list[SQGNode] = Field(min_length=1, max_length=100)
    output_node_id: str
    result_schema: list[ResultColumn] = Field(default_factory=list)


class TokenMetadata(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int


class TimingMetadata(StrictModel):
    initialization_ms: float = Field(ge=0)
    provider_ms: float = Field(ge=0)
    validation_ms: float = Field(ge=0)
    total_ms: float = Field(ge=0)


class CompileResponse(StrictModel):
    api_version: Literal["v1"] = "v1"
    status: CompileStatus
    correlation_id: str
    resolved_terms: list[ResolvedTerm]
    selected_semantic_context: SemanticContext
    candidate_sqg: dict[str, Any] | None
    normalized_sqg: SQG | None
    diagnostics: list[Diagnostic]
    token_metadata: TokenMetadata
    timing_metadata: TimingMetadata
    repair_attempted: bool = False


class ProblemDetails(StrictModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    request_id: str
