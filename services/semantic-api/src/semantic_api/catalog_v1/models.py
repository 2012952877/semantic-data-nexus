"""Additive compiler contracts. No v0 DTO or validator is relaxed."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt

Id = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
Alias = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Scalar = Literal["string", "integer", "number", "boolean", "datetime"]
Value = Annotated[str, Field(max_length=512)] | StrictInt | StrictFloat | StrictBool
Function = Literal["sum", "avg", "min", "max", "count"]


class Frozen(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class Scope(Frozen):
    tenant_id: Id
    workspace_id: Id


class ResourceVersion(Frozen):
    contract_version: Literal["resource-version/v1"] = "resource-version/v1"
    scope: Scope
    resource_kind: Literal["ontology"] = "ontology"
    resource_id: Id
    revision: Annotated[int, Field(strict=True, ge=1)]
    content_sha256: Digest


class Comparison(Frozen):
    kind: Literal["comparison"]
    column: Id
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte"]
    value: Value


class MemberPredicate(Frozen):
    kind: Literal["members"]
    column: Id
    member_ids: Annotated[tuple[Id, ...], Field(min_length=1, max_length=64)]


class TimePredicate(Frozen):
    kind: Literal["time"]
    column: Id
    window_id: Id


Predicate = Annotated[Comparison | MemberPredicate | TimePredicate, Field(discriminator="kind")]


class NamedTerm(Frozen):
    id: Id
    label: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(max_length=2_000)] = ""
    synonyms: Annotated[tuple[str, ...], Field(max_length=16)] = ()


class Member(NamedTerm):
    value: Value


class FieldDefinition(NamedTerm):
    entity_id: Id
    data_type: Scalar
    members: Annotated[tuple[Member, ...], Field(max_length=128)] = ()
    groupable: bool = True
    filterable: bool = True


class Metric(NamedTerm):
    entity_id: Id
    field_id: Id
    function: Function


class Relation(NamedTerm):
    from_entity: Id
    to_entity: Id
    from_field: Id
    to_field: Id
    # Only non-fanout joins are admitted. Publication must verify the key.
    cardinality: Literal["many_to_one", "one_to_one"]


class TimeWindow(NamedTerm):
    start: AwareDatetime
    end_exclusive: AwareDatetime


class Entity(NamedTerm):
    required_filters: Annotated[tuple[Predicate, ...], Field(max_length=16)] = ()


class CatalogDocument(Frozen):
    contract_version: Literal["compiler-catalog/v1"]
    scope: Scope
    resource_id: Id
    revision: Annotated[int, Field(strict=True, ge=1)]
    entities: Annotated[tuple[Entity, ...], Field(min_length=1, max_length=64)]
    fields: Annotated[tuple[FieldDefinition, ...], Field(min_length=1, max_length=256)]
    metrics: Annotated[tuple[Metric, ...], Field(max_length=64)] = ()
    relations: Annotated[tuple[Relation, ...], Field(max_length=64)] = ()
    time_windows: Annotated[tuple[TimeWindow, ...], Field(max_length=32)] = ()


class Select(Frozen):
    kind: Literal["SELECT"]
    entity_id: Id
    columns: Annotated[tuple[Id, ...], Field(min_length=1, max_length=64)]


class Filter(Frozen):
    kind: Literal["FILTER"]
    predicate: Predicate


class Join(Frozen):
    kind: Literal["JOIN"]
    relation_id: Id
    join_type: Literal["inner", "left"]


class Measure(Frozen):
    metric_id: Id
    output: Alias


class Aggregate(Frozen):
    kind: Literal["AGGREGATE"]
    group_by: Annotated[tuple[Id, ...], Field(max_length=16)]
    measures: Annotated[tuple[Measure, ...], Field(min_length=1, max_length=16)]


class Projection(Frozen):
    source: Id
    alias: Alias


class Project(Frozen):
    kind: Literal["PROJECT"]
    columns: Annotated[tuple[Projection, ...], Field(min_length=1, max_length=64)]


class SortKey(Frozen):
    column: Id
    direction: Literal["asc", "desc"]


class Sort(Frozen):
    kind: Literal["SORT"]
    keys: Annotated[tuple[SortKey, ...], Field(min_length=1, max_length=16)]


class Limit(Frozen):
    kind: Literal["LIMIT"]
    count: Annotated[int, Field(strict=True, ge=1, le=10_000)]


Operation = Annotated[
    Select | Filter | Join | Aggregate | Project | Sort | Limit, Field(discriminator="kind")
]


class Node(Frozen):
    id: Id
    dependencies: Annotated[tuple[Id, ...], Field(max_length=2)]
    operation: Operation


class ResultColumn(Frozen):
    name: Alias
    data_type: Scalar


class SQGV1(Frozen):
    contract_version: Literal["sqg/v1"]
    catalog: ResourceVersion
    nodes: Annotated[tuple[Node, ...], Field(min_length=2, max_length=32)]
    output_node_id: Id
    result_schema: Annotated[tuple[ResultColumn, ...], Field(min_length=1, max_length=64)]


class Candidate(Frozen):
    contract_version: Literal["compiler-candidate/v1"]
    status: Literal["graph", "clarification", "blocked"]
    graph: SQGV1 | None
    # A model may select one server-created ambiguity, never invent answer choices.
    ambiguity_id: Id | None


class CatalogCompileRequest(Frozen):
    contract_version: Literal["catalog-compile/v1"]
    request_id: Id
    catalog: ResourceVersion
    question: Annotated[str, Field(min_length=1, max_length=4_000)]


class Choice(Frozen):
    id: Id
    label: Annotated[str, Field(min_length=1, max_length=200)]
    kind: Literal["entity", "field", "metric", "member", "time"]
    target_id: Id
    field_id: Id | None = None


class Ambiguity(Frozen):
    id: Id
    term: Annotated[str, Field(min_length=1, max_length=200)]
    choices: Annotated[tuple[Choice, ...], Field(min_length=2, max_length=32)]


class Resolution(Frozen):
    term: str
    choice: Choice


class CompilerContext(Frozen):
    contract_version: Literal["catalog-context/v1"] = "catalog-context/v1"
    question: str
    catalog: ResourceVersion
    semantic_catalog: CatalogDocument
    resolutions: tuple[Resolution, ...]
    ambiguities: tuple[Ambiguity, ...]
    capabilities: tuple[str, ...]


class CallMetadata(Frozen):
    model: str
    phase: Literal["compile", "repair"]
    outcome: str
    input_tokens: int | None
    output_tokens: int | None


class Compilation(Frozen):
    contract_version: Literal["catalog-compilation/v1"] = "catalog-compilation/v1"
    status: Literal["compiled", "clarification", "blocked"]
    catalog: ResourceVersion
    graph: SQGV1 | None = None
    resolutions: tuple[Resolution, ...] = ()
    clarification_id: Id | None = None
    clarification_revision: int | None = None
    ambiguity: Ambiguity | None = None
    expires_at: datetime | None = None
    diagnostics: tuple[str, ...] = ()
    calls: tuple[CallMetadata, ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None
    repair_attempted: bool = False


class CompilerFailure(ValueError):
    """Value-free failure code: do not disclose question, catalog or provider data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
