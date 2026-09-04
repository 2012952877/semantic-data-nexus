from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from typing import Literal as TypingLiteral

from pydantic import ValidationError

from semantic_api.models import (
    SQG,
    AggregateFunction,
    AggregateParameters,
    BinaryExpression,
    BinaryOperator,
    ColumnExpression,
    CompilationMode,
    DeriveParameters,
    Diagnostic,
    DiagnosticSeverity,
    DiagnosticStage,
    FilterParameters,
    JoinParameters,
    JoinType,
    LiteralExpression,
    Operator,
    PivotParameters,
    PredicateOperator,
    ProjectParameters,
    QueryPolicy,
    ResolvedTerm,
    ResolvedTermKind,
    ResultColumn,
    ScalarType,
    SelectParameters,
    SemanticContext,
    SortParameters,
    SQGNode,
    TimeWindow,
)
from semantic_api.ontology import OntologyRegistry


@dataclass(frozen=True)
class ValidationResult:
    normalized: SQG | None
    diagnostics: list[Diagnostic]

    @property
    def valid(self) -> bool:
        return self.normalized is not None and not any(
            item.severity is DiagnosticSeverity.ERROR for item in self.diagnostics
        )


@dataclass
class _Flow:
    columns: dict[str, _Column]
    grain: set[str]
    entities: frozenset[str]
    used_concepts: frozenset[str] = frozenset()
    enforcing_concepts: frozenset[str] = frozenset()
    member_constraints: tuple[_MemberConstraint, ...] = ()
    time_constraints: tuple[_TimeConstraint, ...] = ()


@dataclass(frozen=True)
class _Column:
    data_type: ScalarType
    entities: frozenset[str]
    source_concept_id: str | None = None
    member_domain_field_id: str | None = None
    time_domain_field_id: str | None = None
    concept_lineage: frozenset[str] = frozenset()
    aggregated_concepts: frozenset[str] = frozenset()
    constraint_eligible: bool = True


@dataclass(frozen=True)
class _MemberConstraint:
    field_id: str
    values: frozenset[str]
    source_enforcing: bool


@dataclass(frozen=True)
class _TimeConstraint:
    field_id: str
    window: tuple[datetime, datetime] | None
    source_enforcing: bool


type _ConceptKind = TypingLiteral["entity", "field", "metric", "relation"]


class SQGValidator:
    def __init__(self, registry: OntologyRegistry) -> None:
        self.registry = registry

    def validate(
        self,
        raw_candidate: Mapping[str, Any],
        context: SemanticContext,
        resolved_terms: list[ResolvedTerm] | None = None,
        time_windows: list[TimeWindow] | None = None,
        compilation_mode: CompilationMode | None = None,
    ) -> ValidationResult:
        try:
            sqg = SQG.model_validate(raw_candidate)
        except ValidationError as error:
            first = error.errors(include_url=False)[0]
            path = ".".join(str(item) for item in first["loc"])
            return ValidationResult(
                normalized=None,
                diagnostics=[
                    self._error(
                        "SCHEMA_INVALID",
                        "Candidate SQG does not match the sqg.v0 schema.",
                        path=path,
                        details={"reason": str(first["type"])},
                    )
                ],
            )

        diagnostics: list[Diagnostic] = []
        node_positions: dict[str, int] = {}
        duplicate_ids: set[str] = set()
        for index, node in enumerate(sqg.nodes):
            if node.id in node_positions:
                duplicate_ids.add(node.id)
                diagnostics.append(
                    self._error(
                        "DUPLICATE_NODE_ID",
                        "Node IDs must be unique.",
                        path=f"nodes.{index}.id",
                        details={"node_id": node.id},
                    )
                )
            else:
                node_positions[node.id] = index

        dependency_structure_valid = not duplicate_ids
        for index, node in enumerate(sqg.nodes):
            if node.operator.value != node.parameters.kind.value:
                diagnostics.append(
                    self._error(
                        "OPERATOR_PARAMETER_MISMATCH",
                        "Node parameters do not match the declared operator.",
                        path=f"nodes.{index}.parameters.kind",
                    )
                )
            seen_dependencies: set[str] = set()
            for dependency in node.dependencies:
                if dependency in seen_dependencies:
                    dependency_structure_valid = False
                    diagnostics.append(
                        self._error(
                            "DUPLICATE_DEPENDENCY",
                            "A dependency may appear only once per node.",
                            path=f"nodes.{index}.dependencies",
                            details={"dependency": dependency},
                        )
                    )
                seen_dependencies.add(dependency)
                if dependency == node.id:
                    dependency_structure_valid = False
                    diagnostics.append(
                        self._error(
                            "SELF_DEPENDENCY",
                            "A node cannot depend on itself.",
                            path=f"nodes.{index}.dependencies",
                        )
                    )
                elif dependency not in node_positions:
                    dependency_structure_valid = False
                    diagnostics.append(
                        self._error(
                            "MISSING_DEPENDENCY",
                            "A dependency references an unknown node.",
                            path=f"nodes.{index}.dependencies",
                            details={"dependency": dependency},
                        )
                    )
                elif node_positions[dependency] >= index:
                    dependency_structure_valid = False
                    diagnostics.append(
                        self._error(
                            "FORWARD_DEPENDENCY",
                            "Dependencies must reference an earlier node.",
                            path=f"nodes.{index}.dependencies",
                            details={"dependency": dependency},
                        )
                    )
            expected = (
                0
                if node.operator is Operator.SELECT
                else 2
                if node.operator is Operator.JOIN
                else 1
            )
            if len(node.dependencies) != expected:
                dependency_structure_valid = False
                diagnostics.append(
                    self._error(
                        "DEPENDENCY_ARITY_INVALID",
                        "Operator has an invalid number of dependencies.",
                        path=f"nodes.{index}.dependencies",
                        details={"expected": expected, "actual": len(node.dependencies)},
                    )
                )

        if sqg.output_node_id not in node_positions:
            dependency_structure_valid = False
            diagnostics.append(
                self._error(
                    "OUTPUT_NODE_MISSING",
                    "The output node does not exist.",
                    path="output_node_id",
                )
            )
        elif dependency_structure_valid:
            reachable = self._ancestors(sqg.output_node_id, sqg.nodes)
            for node in sqg.nodes:
                if node.id not in reachable:
                    diagnostics.append(
                        self._error(
                            "NODE_NOT_OUTPUT_REACHABLE",
                            "Every node must contribute to the declared output.",
                            path=f"nodes.{node_positions[node.id]}.id",
                            details={"node_id": node.id},
                        )
                    )

        selected = {
            item.id
            for items in (context.entities, context.fields, context.metrics, context.relations)
            for item in items
        }
        flows: dict[str, _Flow] = {}
        if dependency_structure_valid:
            for index, node in enumerate(sqg.nodes):
                flow = self._validate_node(node, index, flows, selected, diagnostics)
                if flow is not None:
                    flows[node.id] = flow
            self._validate_constraint_coverage(
                flows.get(sqg.output_node_id),
                resolved_terms or [],
                time_windows or [],
                compilation_mode,
                diagnostics,
            )
            if compilation_mode is not None:
                self._validate_compilation_mode(
                    sqg,
                    flows.get(sqg.output_node_id),
                    compilation_mode,
                    resolved_terms or [],
                    time_windows or [],
                    diagnostics,
                )

        normalized: SQG | None = None
        if sqg.output_node_id in flows:
            output_flow = flows[sqg.output_node_id]
            inferred_schema = [
                ResultColumn(name=name, data_type=column.data_type)
                for name, column in output_flow.columns.items()
            ]
            if sqg.result_schema:
                declared = [(item.name, item.data_type) for item in sqg.result_schema]
                inferred = [(item.name, item.data_type) for item in inferred_schema]
                if declared != inferred:
                    diagnostics.append(
                        self._error(
                            "RESULT_SCHEMA_MISMATCH",
                            "Declared result schema does not match deterministic column flow.",
                            path="result_schema",
                        )
                    )
            if not any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics):
                normalized = sqg.model_copy(
                    update={"result_schema": sqg.result_schema or inferred_schema}
                )
        return ValidationResult(normalized=normalized, diagnostics=diagnostics)

    def _validate_node(
        self,
        node: SQGNode,
        index: int,
        flows: dict[str, _Flow],
        selected: set[str],
        diagnostics: list[Diagnostic],
    ) -> _Flow | None:
        path = f"nodes.{index}"
        if node.operator is Operator.SELECT and isinstance(node.parameters, SelectParameters):
            return self._select_flow(node.parameters, path, selected, diagnostics)

        dependencies = [flows.get(item) for item in node.dependencies]
        if any(item is None for item in dependencies):
            diagnostics.append(
                self._error(
                    "DEPENDENCY_FLOW_UNAVAILABLE",
                    "A dependency did not produce a valid column flow.",
                    path=f"{path}.dependencies",
                )
            )
            return None
        input_flows = [item for item in dependencies if item is not None]

        if node.operator is Operator.FILTER and isinstance(node.parameters, FilterParameters):
            source = input_flows[0]
            self._validate_predicate(node.parameters, source, path, diagnostics)
            return self._filtered_flow(node.parameters, source)
        if node.operator is Operator.AGGREGATE and isinstance(node.parameters, AggregateParameters):
            return self._aggregate_flow(node.parameters, input_flows[0], path, diagnostics)
        if node.operator is Operator.PIVOT and isinstance(node.parameters, PivotParameters):
            return self._pivot_flow(node.parameters, input_flows[0], path, diagnostics)
        if node.operator is Operator.DERIVE and isinstance(node.parameters, DeriveParameters):
            return self._derive_flow(node.parameters, input_flows[0], path, diagnostics)
        if node.operator is Operator.PROJECT and isinstance(node.parameters, ProjectParameters):
            return self._project_flow(node.parameters, input_flows[0], path, diagnostics)
        if node.operator is Operator.SORT and isinstance(node.parameters, SortParameters):
            source = input_flows[0]
            enforcing_concepts = set(source.enforcing_concepts)
            for key_index, key in enumerate(node.parameters.keys):
                column = self._require_column(
                    key.column,
                    source,
                    f"{path}.parameters.keys.{key_index}.column",
                    diagnostics,
                )
                if column is not None:
                    enforcing_concepts.update(column.concept_lineage)
                    enforcing_concepts.update(column.entities)
            return _Flow(
                columns=dict(source.columns),
                grain=set(source.grain),
                entities=source.entities,
                used_concepts=source.used_concepts,
                enforcing_concepts=frozenset(enforcing_concepts),
                member_constraints=source.member_constraints,
                time_constraints=source.time_constraints,
            )
        if node.operator is Operator.JOIN and isinstance(node.parameters, JoinParameters):
            return self._join_flow(node.parameters, input_flows, path, selected, diagnostics)

        diagnostics.append(
            self._error(
                "UNSUPPORTED_OPERATOR",
                "The operator is not supported by sqg.v0 validation.",
                path=f"{path}.operator",
            )
        )
        return None

    def _validate_predicate(
        self,
        parameters: FilterParameters,
        source: _Flow,
        path: str,
        diagnostics: list[Diagnostic],
    ) -> None:
        value_path = f"{path}.parameters.predicate.value"
        column = self._require_column(
            parameters.predicate.column,
            source,
            f"{path}.parameters.predicate.column",
            diagnostics,
        )
        if column is None:
            return
        operator = parameters.predicate.operator
        raw_value = parameters.predicate.value
        if column.member_domain_field_id is not None and operator not in (
            PredicateOperator.EQ,
            PredicateOperator.IN,
        ):
            diagnostics.append(
                self._error(
                    "MEMBER_OPERATOR_UNSUPPORTED",
                    "Resolved member filters support only positive EQ or IN predicates.",
                    path=f"{path}.parameters.predicate.operator",
                )
            )
        if operator is PredicateOperator.IN:
            if not isinstance(raw_value, list) or not raw_value:
                diagnostics.append(
                    self._error(
                        "PREDICATE_CARDINALITY_INVALID",
                        "IN predicates require a non-empty list.",
                        path=value_path,
                    )
                )
                return
            values: Sequence[object] = raw_value
        elif operator is PredicateOperator.BETWEEN:
            self._validate_between(raw_value, column, value_path, diagnostics)
            return
        else:
            if isinstance(raw_value, (list, dict)):
                diagnostics.append(
                    self._error(
                        "PREDICATE_CARDINALITY_INVALID",
                        "This predicate operator requires one scalar value.",
                        path=value_path,
                    )
                )
                return
            values = [raw_value]

        for value_index, value in enumerate(values):
            item_path = f"{value_path}.{value_index}" if isinstance(raw_value, list) else value_path
            if (
                column.data_type is ScalarType.DATETIME
                and value is not None
                and self._parse_datetime(value) is None
            ):
                diagnostics.append(
                    self._error(
                        "DATETIME_LITERAL_INVALID",
                        "Datetime predicate values must be timezone-aware ISO 8601 strings.",
                        path=item_path,
                    )
                )
            elif value is not None and not self._runtime_value_matches(value, column.data_type):
                diagnostics.append(
                    self._error(
                        "PREDICATE_VALUE_TYPE_INVALID",
                        "Predicate value does not match the input column type.",
                        path=item_path,
                    )
                )
            if value is None and operator not in (PredicateOperator.EQ, PredicateOperator.NE):
                diagnostics.append(
                    self._error(
                        "PREDICATE_VALUE_TYPE_INVALID",
                        "Only equality predicates may compare with null.",
                        path=item_path,
                    )
                )
            if column.member_domain_field_id is None:
                continue
            if not isinstance(value, str) or not self.registry.has_member(value):
                diagnostics.append(
                    self._error(
                        "ONTOLOGY_MEMBER_UNKNOWN",
                        "Member predicates require an exact ontology member ID.",
                        path=item_path,
                    )
                )
                continue
            member_field_id, member = self.registry.members[value]
            if member_field_id != column.member_domain_field_id:
                diagnostics.append(
                    self._error(
                        "MEMBER_FIELD_MISMATCH",
                        "Ontology member does not belong to the filtered field.",
                        path=item_path,
                        details={"member_id": value},
                    )
                )
            elif not member.enabled:
                diagnostics.append(
                    self._error(
                        "ONTOLOGY_CONCEPT_FORBIDDEN",
                        "Ontology member is disabled.",
                        path=item_path,
                        details={"concept_id": value},
                    )
                )

    def _validate_between(
        self,
        raw_value: object,
        column: _Column,
        path: str,
        diagnostics: list[Diagnostic],
    ) -> None:
        if (
            not isinstance(raw_value, dict)
            or set(raw_value) != {"start", "end_exclusive"}
            or column.data_type is not ScalarType.DATETIME
        ):
            diagnostics.append(
                self._error(
                    "PREDICATE_CARDINALITY_INVALID",
                    "BETWEEN requires a datetime range with start and end_exclusive.",
                    path=path,
                )
            )
            return
        start = self._parse_datetime(raw_value["start"])
        end = self._parse_datetime(raw_value["end_exclusive"])
        if start is None or end is None:
            diagnostics.append(
                self._error(
                    "DATETIME_LITERAL_INVALID",
                    "Datetime predicate values must be timezone-aware ISO 8601 strings.",
                    path=path,
                )
            )
            return
        start_utc = self._as_utc(start)
        end_utc = self._as_utc(end)
        if start_utc is None or end_utc is None:
            diagnostics.append(
                self._error(
                    "DATETIME_INSTANT_OUT_OF_RANGE",
                    "Datetime boundaries cannot be represented as UTC instants.",
                    path=path,
                )
            )
        elif start_utc >= end_utc:
            diagnostics.append(
                self._error(
                    "TIME_RANGE_INVALID",
                    "Datetime range start must be before end_exclusive.",
                    path=path,
                )
            )

    def _filtered_flow(self, parameters: FilterParameters, source: _Flow) -> _Flow:
        member_constraints = source.member_constraints
        time_constraints = source.time_constraints
        column = source.columns.get(parameters.predicate.column)
        if column is None:
            return _Flow(
                columns=dict(source.columns),
                grain=set(source.grain),
                entities=source.entities,
                used_concepts=source.used_concepts,
                enforcing_concepts=source.enforcing_concepts,
                member_constraints=member_constraints,
                time_constraints=time_constraints,
            )
        raw_value = parameters.predicate.value
        enforcing_concepts = source.enforcing_concepts | column.concept_lineage | column.entities
        if column.member_domain_field_id is not None and parameters.predicate.operator in (
            PredicateOperator.EQ,
            PredicateOperator.IN,
        ):
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            member_constraints += (
                _MemberConstraint(
                    field_id=column.member_domain_field_id,
                    values=frozenset(
                        value
                        for value in values
                        if isinstance(value, str) and self.registry.has_member(value)
                    ),
                    source_enforcing=column.constraint_eligible,
                ),
            )
        if column.time_domain_field_id is not None:
            parsed_window: tuple[datetime, datetime] | None = None
            if (
                parameters.predicate.operator is PredicateOperator.BETWEEN
                and isinstance(raw_value, dict)
                and set(raw_value) == {"start", "end_exclusive"}
            ):
                start = self._parse_datetime(raw_value["start"])
                end = self._parse_datetime(raw_value["end_exclusive"])
                start_utc = self._as_utc(start) if start is not None else None
                end_utc = self._as_utc(end) if end is not None else None
                if start_utc is not None and end_utc is not None and start_utc < end_utc:
                    parsed_window = (start_utc, end_utc)
            time_constraints += (
                _TimeConstraint(
                    field_id=column.time_domain_field_id,
                    window=parsed_window,
                    source_enforcing=column.constraint_eligible,
                ),
            )
        return _Flow(
            columns=dict(source.columns),
            grain=set(source.grain),
            entities=source.entities,
            used_concepts=source.used_concepts,
            enforcing_concepts=enforcing_concepts,
            member_constraints=member_constraints,
            time_constraints=time_constraints,
        )

    def _select_flow(
        self,
        parameters: SelectParameters,
        path: str,
        selected: set[str],
        diagnostics: list[Diagnostic],
    ) -> _Flow:
        self._validate_concept(
            parameters.entity_id,
            "entity",
            selected,
            f"{path}.parameters.entity_id",
            diagnostics,
        )
        columns: dict[str, _Column] = {}
        grain: set[str] = set()
        for column_index, column in enumerate(parameters.columns):
            column_path = f"{path}.parameters.columns.{column_index}"
            if column in columns:
                diagnostics.append(
                    self._error(
                        "DUPLICATE_OUTPUT_COLUMN",
                        "An operator cannot emit duplicate column names.",
                        path=column_path,
                        details={"column": column},
                    )
                )
                continue
            if column in self.registry.typed_fields():
                field = self.registry.typed_fields()[column]
                self._validate_concept(column, "field", selected, column_path, diagnostics)
                if field.entity_id != parameters.entity_id:
                    diagnostics.append(
                        self._error(
                            "FIELD_ENTITY_MISMATCH",
                            "Selected field does not belong to the selected entity.",
                            path=column_path,
                        )
                    )
                columns[column] = _Column(
                    data_type=field.data_type,
                    entities=frozenset({field.entity_id}),
                    source_concept_id=field.id,
                    member_domain_field_id=field.id if field.members else None,
                    time_domain_field_id=(
                        field.id if field.data_type is ScalarType.DATETIME else None
                    ),
                    concept_lineage=frozenset({field.id}),
                )
                grain.add(column)
            elif column in self.registry.typed_metrics():
                metric = self.registry.typed_metrics()[column]
                self._validate_concept(column, "metric", selected, column_path, diagnostics)
                if metric.entity_id != parameters.entity_id:
                    diagnostics.append(
                        self._error(
                            "METRIC_ENTITY_MISMATCH",
                            "Selected metric does not belong to the selected entity.",
                            path=column_path,
                        )
                    )
                columns[column] = _Column(
                    data_type=metric.data_type,
                    entities=frozenset({metric.entity_id}),
                    source_concept_id=metric.id,
                    concept_lineage=frozenset({metric.id}),
                )
            else:
                diagnostics.append(
                    self._error(
                        "ONTOLOGY_CONCEPT_UNKNOWN",
                        "Selected column is not an exact ontology field or metric ID.",
                        path=column_path,
                        details={"concept_id": column},
                    )
                )
        return _Flow(
            columns=columns,
            grain=grain,
            entities=frozenset({parameters.entity_id}),
            used_concepts=frozenset({parameters.entity_id, *columns}),
        )

    def _aggregate_flow(
        self,
        parameters: AggregateParameters,
        source: _Flow,
        path: str,
        diagnostics: list[Diagnostic],
    ) -> _Flow:
        columns: dict[str, _Column] = {}
        enforcing_concepts = set(source.enforcing_concepts)
        for group_index, group in enumerate(parameters.group_by):
            column_info = self._require_column(
                group,
                source,
                f"{path}.parameters.group_by.{group_index}",
                diagnostics,
            )
            if group in columns:
                diagnostics.append(
                    self._error(
                        "DUPLICATE_OUTPUT_COLUMN",
                        "Aggregate group columns must be unique.",
                        path=f"{path}.parameters.group_by.{group_index}",
                    )
                )
            elif column_info is not None:
                columns[group] = column_info
                enforcing_concepts.update(column_info.concept_lineage)
                enforcing_concepts.update(column_info.entities)
        for measure_index, measure in enumerate(parameters.measures):
            measure_path = f"{path}.parameters.measures.{measure_index}"
            source_info = self._require_column(
                measure.source, source, f"{measure_path}.source", diagnostics
            )
            if source_info is not None and source_info.data_type is not ScalarType.NUMBER:
                diagnostics.append(
                    self._error(
                        "AGGREGATE_TYPE_INVALID",
                        "Aggregate measures require numeric input.",
                        path=f"{measure_path}.source",
                    )
                )
            if measure.output in columns:
                diagnostics.append(
                    self._error(
                        "DUPLICATE_OUTPUT_COLUMN",
                        "Aggregate output collides with another output column.",
                        path=f"{measure_path}.output",
                        details={"column": measure.output},
                    )
                )
            else:
                columns[measure.output] = _Column(
                    data_type=ScalarType.NUMBER,
                    entities=(source_info.entities if source_info is not None else source.entities),
                    concept_lineage=(
                        source_info.concept_lineage if source_info is not None else frozenset()
                    ),
                    aggregated_concepts=(
                        source_info.concept_lineage | source_info.aggregated_concepts
                        if source_info is not None
                        else frozenset()
                    ),
                    constraint_eligible=False,
                )
        return _Flow(
            columns=columns,
            grain=set(parameters.group_by),
            entities=source.entities,
            used_concepts=source.used_concepts,
            enforcing_concepts=frozenset(enforcing_concepts),
            member_constraints=source.member_constraints,
            time_constraints=source.time_constraints,
        )

    def _pivot_flow(
        self,
        parameters: PivotParameters,
        source: _Flow,
        path: str,
        diagnostics: list[Diagnostic],
    ) -> _Flow:
        columns: dict[str, _Column] = {}
        enforcing_concepts = set(source.enforcing_concepts)
        for index_position, column in enumerate(parameters.index):
            column_info = self._require_column(
                column, source, f"{path}.parameters.index.{index_position}", diagnostics
            )
            if column_info is not None:
                columns[column] = column_info
                enforcing_concepts.update(column_info.concept_lineage)
                enforcing_concepts.update(column_info.entities)
        pivot_column = self._require_column(
            parameters.column, source, f"{path}.parameters.column", diagnostics
        )
        value_info = self._require_column(
            parameters.value, source, f"{path}.parameters.value", diagnostics
        )
        for column_info in (pivot_column, value_info):
            if column_info is not None:
                enforcing_concepts.update(column_info.concept_lineage)
                enforcing_concepts.update(column_info.entities)
        for value_index, value in enumerate(parameters.values):
            if value in columns:
                diagnostics.append(
                    self._error(
                        "DUPLICATE_OUTPUT_COLUMN",
                        "Pivot output collides with an index column.",
                        path=f"{path}.parameters.values.{value_index}",
                        details={"column": value},
                    )
                )
            else:
                columns[value] = _Column(
                    data_type=(
                        value_info.data_type if value_info is not None else ScalarType.NUMBER
                    ),
                    entities=(value_info.entities if value_info is not None else source.entities),
                    member_domain_field_id=(
                        value_info.member_domain_field_id if value_info is not None else None
                    ),
                    time_domain_field_id=(
                        value_info.time_domain_field_id if value_info is not None else None
                    ),
                    concept_lineage=(
                        value_info.concept_lineage if value_info is not None else frozenset()
                    ),
                    aggregated_concepts=(
                        value_info.aggregated_concepts if value_info is not None else frozenset()
                    ),
                    constraint_eligible=False,
                )
        if len(set(parameters.values)) != len(parameters.values):
            diagnostics.append(
                self._error(
                    "DUPLICATE_OUTPUT_COLUMN",
                    "Pivot values must produce unique columns.",
                    path=f"{path}.parameters.values",
                )
            )
        return _Flow(
            columns=columns,
            grain=set(parameters.index),
            entities=source.entities,
            used_concepts=source.used_concepts,
            enforcing_concepts=frozenset(enforcing_concepts),
            member_constraints=source.member_constraints,
            time_constraints=source.time_constraints,
        )

    def _derive_flow(
        self,
        parameters: DeriveParameters,
        source: _Flow,
        path: str,
        diagnostics: list[Diagnostic],
    ) -> _Flow:
        columns = dict(source.columns)
        for column_index, derived in enumerate(parameters.columns):
            column_path = f"{path}.parameters.columns.{column_index}"
            if derived.output in columns:
                diagnostics.append(
                    self._error(
                        "DERIVE_OUTPUT_COLLISION",
                        "Derived output cannot replace an existing column.",
                        path=f"{column_path}.output",
                        details={"column": derived.output},
                    )
                )
                continue
            expression_info = self._expression_info(
                derived.expression, source, f"{column_path}.expression", diagnostics
            )
            if expression_info is not None and expression_info.data_type is not derived.data_type:
                diagnostics.append(
                    self._error(
                        "DERIVE_TYPE_MISMATCH",
                        "Derived output type does not match its expression.",
                        path=f"{column_path}.data_type",
                    )
                )
            columns[derived.output] = _Column(
                data_type=derived.data_type,
                entities=(
                    expression_info.entities if expression_info is not None else source.entities
                ),
                member_domain_field_id=(
                    expression_info.member_domain_field_id if expression_info is not None else None
                ),
                time_domain_field_id=(
                    expression_info.time_domain_field_id if expression_info is not None else None
                ),
                concept_lineage=(
                    expression_info.concept_lineage if expression_info is not None else frozenset()
                ),
                aggregated_concepts=(
                    expression_info.aggregated_concepts
                    if expression_info is not None
                    else frozenset()
                ),
                constraint_eligible=False,
            )
        return _Flow(
            columns=columns,
            grain=set(source.grain),
            entities=source.entities,
            used_concepts=source.used_concepts,
            enforcing_concepts=source.enforcing_concepts,
            member_constraints=source.member_constraints,
            time_constraints=source.time_constraints,
        )

    def _project_flow(
        self,
        parameters: ProjectParameters,
        source: _Flow,
        path: str,
        diagnostics: list[Diagnostic],
    ) -> _Flow:
        columns: dict[str, _Column] = {}
        grain: set[str] = set()
        for projection_index, projection in enumerate(parameters.columns):
            projection_path = f"{path}.parameters.columns.{projection_index}"
            column_info = self._require_column(
                projection.source, source, f"{projection_path}.source", diagnostics
            )
            if projection.alias in columns:
                diagnostics.append(
                    self._error(
                        "DUPLICATE_OUTPUT_COLUMN",
                        "Projected aliases must be unique.",
                        path=f"{projection_path}.alias",
                    )
                )
            elif column_info is not None:
                columns[projection.alias] = column_info
                if projection.source in source.grain:
                    grain.add(projection.alias)
        if source.grain and not grain:
            diagnostics.append(
                self._error(
                    "GRAIN_LOST",
                    "Projection removed every grain column.",
                    path=f"{path}.parameters.columns",
                )
            )
        return _Flow(
            columns=columns,
            grain=grain,
            entities=source.entities,
            used_concepts=source.used_concepts,
            enforcing_concepts=source.enforcing_concepts,
            member_constraints=source.member_constraints,
            time_constraints=source.time_constraints,
        )

    def _join_flow(
        self,
        parameters: JoinParameters,
        inputs: list[_Flow],
        path: str,
        selected: set[str],
        diagnostics: list[Diagnostic],
    ) -> _Flow:
        self._validate_concept(
            parameters.relation_id,
            "relation",
            selected,
            f"{path}.parameters.relation_id",
            diagnostics,
        )
        left_key = self._require_column(
            parameters.left_key, inputs[0], f"{path}.parameters.left_key", diagnostics
        )
        right_key = self._require_column(
            parameters.right_key, inputs[1], f"{path}.parameters.right_key", diagnostics
        )
        relation = self.registry.typed_relations().get(parameters.relation_id)
        if relation is not None:
            if (
                relation.from_entity_id not in inputs[0].entities
                or relation.to_entity_id not in inputs[1].entities
            ):
                diagnostics.append(
                    self._error(
                        "RELATION_ENDPOINT_MISMATCH",
                        "Join inputs do not match the relation direction and endpoints.",
                        path=f"{path}.dependencies",
                    )
                )
            if (left_key is not None and left_key.source_concept_id != relation.from_field_id) or (
                right_key is not None and right_key.source_concept_id != relation.to_field_id
            ):
                diagnostics.append(
                    self._error(
                        "JOIN_KEY_NOT_GOVERNED",
                        "Join keys must preserve the governed relation key provenance.",
                        path=f"{path}.parameters",
                    )
                )
            if (
                left_key is not None
                and right_key is not None
                and left_key.data_type is not right_key.data_type
            ):
                diagnostics.append(
                    self._error(
                        "JOIN_KEY_TYPE_MISMATCH",
                        "Join key types must match.",
                        path=f"{path}.parameters",
                    )
                )
        columns = dict(inputs[0].columns)
        for column, column_info in inputs[1].columns.items():
            if column in columns:
                diagnostics.append(
                    self._error(
                        "JOIN_OUTPUT_COLLISION",
                        "Join inputs contain an unqualified duplicate column.",
                        path=f"{path}.parameters",
                        details={"column": column},
                    )
                )
            else:
                columns[column] = column_info
        member_constraints = inputs[0].member_constraints
        time_constraints = inputs[0].time_constraints
        enforcing_concepts = set(inputs[0].enforcing_concepts | inputs[1].enforcing_concepts)
        enforcing_concepts.add(parameters.relation_id)
        for key in (left_key, right_key):
            if key is not None:
                enforcing_concepts.update(key.concept_lineage)
                enforcing_concepts.update(key.entities)
        enforcing_concepts.update(inputs[0].entities)
        enforcing_concepts.update(inputs[1].entities)
        if parameters.join_type is JoinType.INNER:
            member_constraints += inputs[1].member_constraints
            time_constraints += inputs[1].time_constraints
        else:
            member_constraints += tuple(
                _MemberConstraint(
                    field_id=item.field_id,
                    values=item.values,
                    source_enforcing=False,
                )
                for item in inputs[1].member_constraints
            )
            time_constraints += tuple(
                _TimeConstraint(
                    field_id=item.field_id,
                    window=item.window,
                    source_enforcing=False,
                )
                for item in inputs[1].time_constraints
            )
        return _Flow(
            columns=columns,
            grain=inputs[0].grain | inputs[1].grain,
            entities=inputs[0].entities | inputs[1].entities,
            used_concepts=(
                inputs[0].used_concepts
                | inputs[1].used_concepts
                | frozenset({parameters.relation_id})
            ),
            enforcing_concepts=frozenset(enforcing_concepts),
            member_constraints=member_constraints,
            time_constraints=time_constraints,
        )

    def _expression_info(
        self,
        expression: ColumnExpression | LiteralExpression | BinaryExpression,
        source: _Flow,
        path: str,
        diagnostics: list[Diagnostic],
    ) -> _Column | None:
        if isinstance(expression, ColumnExpression):
            return self._require_column(expression.column, source, f"{path}.column", diagnostics)
        if isinstance(expression, LiteralExpression):
            if expression.value is not None and not self._runtime_value_matches(
                expression.value, expression.data_type
            ):
                diagnostics.append(
                    self._error(
                        "LITERAL_VALUE_TYPE_INVALID",
                        "Literal runtime value does not match its declared type.",
                        path=f"{path}.value",
                    )
                )
            if (
                expression.data_type is ScalarType.DATETIME
                and expression.value is not None
                and self._parse_datetime(expression.value) is None
            ):
                diagnostics.append(
                    self._error(
                        "DATETIME_LITERAL_INVALID",
                        "Datetime literals must be timezone-aware ISO 8601 strings.",
                        path=f"{path}.value",
                    )
                )
            return _Column(expression.data_type, frozenset())
        left_info = self._expression_info(expression.left, source, f"{path}.left", diagnostics)
        right_info = self._expression_info(expression.right, source, f"{path}.right", diagnostics)
        if left_info is not None and left_info.data_type is not ScalarType.NUMBER:
            diagnostics.append(
                self._error(
                    "EXPRESSION_TYPE_INVALID",
                    "Binary arithmetic requires numeric operands.",
                    path=f"{path}.left",
                )
            )
        if right_info is not None and right_info.data_type is not ScalarType.NUMBER:
            diagnostics.append(
                self._error(
                    "EXPRESSION_TYPE_INVALID",
                    "Binary arithmetic requires numeric operands.",
                    path=f"{path}.right",
                )
            )
        return _Column(
            ScalarType.NUMBER,
            (left_info.entities if left_info is not None else frozenset())
            | (right_info.entities if right_info is not None else frozenset()),
            concept_lineage=(left_info.concept_lineage if left_info is not None else frozenset())
            | (right_info.concept_lineage if right_info is not None else frozenset()),
            aggregated_concepts=(
                left_info.aggregated_concepts if left_info is not None else frozenset()
            )
            | (right_info.aggregated_concepts if right_info is not None else frozenset()),
            constraint_eligible=False,
        )

    def _validate_concept(
        self,
        machine_id: str,
        expected_kind: _ConceptKind,
        selected: set[str],
        path: str,
        diagnostics: list[Diagnostic],
    ) -> None:
        membership = {
            "entity": self.registry.has_entity,
            "field": self.registry.has_field,
            "metric": self.registry.has_metric,
            "relation": self.registry.has_relation,
        }[expected_kind]
        if not membership(machine_id):
            diagnostics.append(
                self._error(
                    "ONTOLOGY_CONCEPT_UNKNOWN",
                    f"Reference is not an exact ontology {expected_kind} ID.",
                    path=path,
                    details={"concept_id": machine_id},
                )
            )
            return
        state = self.registry.concept_state(machine_id, expected_kind)
        if state is not None and (not state[0] or state[1] is QueryPolicy.DENY):
            diagnostics.append(
                self._error(
                    "ONTOLOGY_CONCEPT_FORBIDDEN",
                    "Ontology concept is disabled or denied by query policy.",
                    path=path,
                    details={"concept_id": machine_id},
                )
            )
        if machine_id not in selected:
            diagnostics.append(
                self._error(
                    "CONTEXT_CONCEPT_NOT_SELECTED",
                    "Ontology concept was not included in the retrieved semantic context.",
                    path=path,
                    details={"concept_id": machine_id},
                )
            )

    def _require_column(
        self, column: str, flow: _Flow, path: str, diagnostics: list[Diagnostic]
    ) -> _Column | None:
        column_info = flow.columns.get(column)
        if column_info is None:
            diagnostics.append(
                self._error(
                    "COLUMN_NOT_AVAILABLE",
                    "Operator references a column not produced by its dependency.",
                    path=path,
                    details={"column": column},
                )
            )
        return column_info

    def _validate_constraint_coverage(
        self,
        output_flow: _Flow | None,
        resolved_terms: list[ResolvedTerm],
        time_windows: list[TimeWindow],
        compilation_mode: CompilationMode | None,
        diagnostics: list[Diagnostic],
    ) -> None:
        expected_members = {
            term.machine_id for term in resolved_terms if term.kind is ResolvedTermKind.MEMBER
        }
        expected_windows: set[tuple[datetime, datetime]] = set()
        for index, window in enumerate(time_windows):
            start_utc = self._as_utc(window.start)
            end_utc = self._as_utc(window.end_exclusive)
            if start_utc is None or end_utc is None:
                diagnostics.append(
                    self._error(
                        "DATETIME_INSTANT_OUT_OF_RANGE",
                        "Normalized time boundaries cannot be represented as UTC instants.",
                        path=f"time_windows.{index}",
                    )
                )
                continue
            expected_windows.add((start_utc, end_utc))
        expected_filter_windows = expected_windows
        if (
            compilation_mode is CompilationMode.MONTHLY_REGIONAL_COMPARISON
            and len(expected_windows) == 2
        ):
            ordered_windows = sorted(expected_windows)
            if ordered_windows[0][1] == ordered_windows[1][0]:
                expected_filter_windows = {(ordered_windows[0][0], ordered_windows[1][1])}
        member_predicates: dict[str, list[set[str]]] = {}
        time_predicates: list[tuple[datetime, datetime] | None] = []
        enforced_members: set[str] = set()
        introduced_members: set[str] = set()
        enforced_windows: set[tuple[datetime, datetime]] = set()
        introduced_windows: set[tuple[datetime, datetime]] = set()
        expected_time_fields = (
            {"commerce.sales_record.period"}
            if compilation_mode is not None and expected_windows
            else {
                term.machine_id
                for term in resolved_terms
                if term.kind is ResolvedTermKind.FIELD
                and term.machine_id in self.registry.typed_fields()
                and self.registry.typed_fields()[term.machine_id].data_type is ScalarType.DATETIME
            }
        )
        if expected_windows and not expected_time_fields:
            expected_time_fields = {"commerce.sales_record.period"}
        if output_flow is not None:
            for member_constraint in output_flow.member_constraints:
                member_predicates.setdefault(member_constraint.field_id, []).append(
                    set(member_constraint.values)
                )
                introduced_members.update(member_constraint.values)
                if member_constraint.source_enforcing:
                    enforced_members.update(member_constraint.values)
            for time_constraint in output_flow.time_constraints:
                time_predicates.append(time_constraint.window)
                if time_constraint.window is not None:
                    introduced_windows.add(time_constraint.window)
                if (
                    time_constraint.source_enforcing
                    and time_constraint.window is not None
                    and (
                        not expected_time_fields or time_constraint.field_id in expected_time_fields
                    )
                ):
                    enforced_windows.add(time_constraint.window)
                if expected_time_fields and time_constraint.field_id not in expected_time_fields:
                    diagnostics.append(
                        self._error(
                            "UNRESOLVED_TIME_DOMAIN",
                            "Time constraint targets a field outside the normalized time domain.",
                            path="nodes",
                            details={"field_id": time_constraint.field_id},
                        )
                    )

        expected_concepts = {
            term.machine_id
            for term in resolved_terms
            if term.kind
            in (
                ResolvedTermKind.ENTITY,
                ResolvedTermKind.FIELD,
                ResolvedTermKind.METRIC,
            )
        }
        output_lineage = (
            frozenset().union(*(column.concept_lineage for column in output_flow.columns.values()))
            if output_flow is not None
            else frozenset()
        )
        output_entities = (
            frozenset().union(*(column.entities for column in output_flow.columns.values()))
            if output_flow is not None
            else frozenset()
        )
        contributing_concepts = (
            output_lineage | output_entities | output_flow.enforcing_concepts
            if output_flow is not None
            else frozenset()
        )
        for concept_id in sorted(expected_concepts - contributing_concepts):
            diagnostics.append(
                self._error(
                    "MISSING_RESOLVED_CONCEPT",
                    "A resolved semantic concept does not contribute to the candidate output.",
                    path="nodes",
                    details={"concept_id": concept_id},
                )
            )

        expected_members_by_field: dict[str, set[str]] = {}
        for member_id in expected_members:
            member_entry = self.registry.members.get(member_id)
            if member_entry is not None:
                expected_members_by_field.setdefault(member_entry[0], set()).add(member_id)
        for field_id, predicates in sorted(member_predicates.items()):
            if len(predicates) != 1:
                diagnostics.append(
                    self._error(
                        "MEMBER_CONSTRAINT_SHAPE_INVALID",
                        "Each governed member domain requires one canonical EQ or IN predicate.",
                        path="nodes",
                        details={
                            "field_id": field_id,
                            "predicate_count": len(predicates),
                        },
                    )
                )
            elif (
                field_id in expected_members_by_field
                and predicates[0] != (expected_members_by_field[field_id])
            ):
                diagnostics.append(
                    self._error(
                        "MEMBER_CONSTRAINT_SHAPE_INVALID",
                        "Member predicate set must exactly match resolved members for its domain.",
                        path="nodes",
                        details={"field_id": field_id},
                    )
                )

        for member_id in sorted(expected_members - enforced_members):
            diagnostics.append(
                self._error(
                    "MISSING_MEMBER_CONSTRAINT",
                    "A resolved member is not enforced by the candidate SQG.",
                    path="nodes",
                    details={"member_id": member_id},
                )
            )
        for member_id in sorted(introduced_members - expected_members):
            diagnostics.append(
                self._error(
                    "UNRESOLVED_MEMBER_CONSTRAINT",
                    "Candidate SQG introduced a member not resolved from the request.",
                    path="nodes",
                    details={"member_id": member_id},
                )
            )
        if expected_filter_windows - enforced_windows:
            diagnostics.append(
                self._error(
                    "MISSING_TIME_CONSTRAINT",
                    "A normalized time window is not enforced by the candidate SQG.",
                    path="nodes",
                    details={"window_count": len(expected_filter_windows - enforced_windows)},
                )
            )
        if introduced_windows - expected_filter_windows:
            diagnostics.append(
                self._error(
                    "UNRESOLVED_TIME_CONSTRAINT",
                    "Candidate SQG introduced a time window not normalized from the request.",
                    path="nodes",
                    details={"window_count": len(introduced_windows - expected_filter_windows)},
                )
            )
        if len(time_predicates) != len(expected_filter_windows) or any(
            item is None for item in time_predicates
        ):
            diagnostics.append(
                self._error(
                    "TIME_CONSTRAINT_SHAPE_INVALID",
                    "Time constraints require exactly one canonical governed BETWEEN predicate.",
                    path="nodes",
                    details={
                        "expected_count": len(expected_filter_windows),
                        "predicate_count": len(time_predicates),
                    },
                )
            )

    def _validate_compilation_mode(
        self,
        sqg: SQG,
        output_flow: _Flow | None,
        mode: CompilationMode,
        resolved_terms: list[ResolvedTerm],
        time_windows: list[TimeWindow],
        diagnostics: list[Diagnostic],
    ) -> None:
        if output_flow is None:
            return
        required_concepts = {
            "commerce.sales_record.region",
            "commerce.sales_record.period",
            "metric.profit",
        }
        for concept_id in sorted(required_concepts - output_flow.used_concepts):
            diagnostics.append(
                self._error(
                    "COMPILATION_MODE_CONCEPT_MISSING",
                    "Candidate omits a concept required by the compilation mode.",
                    path="nodes",
                    details={"concept_id": concept_id, "mode": mode.value},
                )
            )

        expected_schema = (
            [
                ("region", ScalarType.STRING),
                ("period", ScalarType.DATETIME),
                ("profit", ScalarType.NUMBER),
            ]
            if mode is CompilationMode.REGIONAL_QUARTERLY_PROFIT
            else [
                ("region", ScalarType.STRING),
                ("profit_current", ScalarType.NUMBER),
                ("profit_previous", ScalarType.NUMBER),
                ("profit_change", ScalarType.NUMBER),
            ]
        )
        actual_schema = [(name, column.data_type) for name, column in output_flow.columns.items()]
        if actual_schema != expected_schema:
            diagnostics.append(
                self._error(
                    "COMPILATION_MODE_SCHEMA_INVALID",
                    "Output schema does not match the trusted compilation mode contract.",
                    path="result_schema",
                    details={"mode": mode.value},
                )
            )
        required_output_lineage = {
            "region": "commerce.sales_record.region",
            **(
                {
                    "period": "commerce.sales_record.period",
                    "profit": "metric.profit",
                }
                if mode is CompilationMode.REGIONAL_QUARTERLY_PROFIT
                else {
                    "profit_current": "metric.profit",
                    "profit_previous": "metric.profit",
                    "profit_change": "metric.profit",
                }
            ),
        }
        for column_name, concept_id in required_output_lineage.items():
            column = output_flow.columns.get(column_name)
            requires_aggregation = concept_id == "metric.profit"
            if (
                column is None
                or concept_id not in column.concept_lineage
                or (requires_aggregation and concept_id not in column.aggregated_concepts)
            ):
                diagnostics.append(
                    self._error(
                        "COMPILATION_MODE_OUTPUT_LINEAGE_INVALID",
                        "Trusted output is not derived from its required semantic concept.",
                        path="result_schema",
                        details={
                            "column": column_name,
                            "concept_id": concept_id,
                            "mode": mode.value,
                        },
                    )
                )

        node_by_id = {node.id: node for node in sqg.nodes}
        linear_path: list[SQGNode] = []
        current = node_by_id.get(sqg.output_node_id)
        while current is not None:
            linear_path.append(current)
            if not current.dependencies:
                break
            if len(current.dependencies) != 1:
                linear_path = []
                break
            current = node_by_id.get(current.dependencies[0])
        linear_path.reverse()
        operators = [node.operator for node in linear_path]
        filter_count = 0
        if operators and operators[0] is Operator.SELECT:
            filter_count = next(
                (
                    index - 1
                    for index, operator in enumerate(operators[1:], start=1)
                    if operator is not Operator.FILTER
                ),
                len(operators) - 1,
            )
        expected_tail = (
            [Operator.AGGREGATE, Operator.SORT, Operator.PROJECT]
            if mode is CompilationMode.REGIONAL_QUARTERLY_PROFIT
            else [
                Operator.AGGREGATE,
                Operator.PIVOT,
                Operator.DERIVE,
                Operator.PROJECT,
            ]
        )
        exact_topology = len(linear_path) == len(sqg.nodes) and operators == [
            Operator.SELECT,
            *([Operator.FILTER] * filter_count),
            *expected_tail,
        ]
        if not exact_topology:
            diagnostics.append(
                self._error(
                    "COMPILATION_MODE_TOPOLOGY_INVALID",
                    "Candidate operator topology is not permitted by the trusted mode.",
                    path="nodes",
                    details={"mode": mode.value},
                )
            )
        allowed_filter_columns = {
            "commerce.sales_record.region"
            for term in resolved_terms
            if term.kind is ResolvedTermKind.MEMBER
        }
        if time_windows:
            allowed_filter_columns.add("commerce.sales_record.period")
        for node in sqg.nodes:
            if (
                isinstance(node.parameters, FilterParameters)
                and node.parameters.predicate.column not in allowed_filter_columns
            ):
                diagnostics.append(
                    self._error(
                        "COMPILATION_MODE_FILTER_INVALID",
                        "Trusted modes allow only filters backed by resolved constraints.",
                        path="nodes",
                        details={
                            "column": node.parameters.predicate.column,
                            "mode": mode.value,
                        },
                    )
                )
        aggregate_parameters = [
            node.parameters
            for node in sqg.nodes
            if node.operator is Operator.AGGREGATE
            and isinstance(node.parameters, AggregateParameters)
        ]
        expected_group_by = [
            "commerce.sales_record.region",
            "commerce.sales_record.period",
        ]
        exact_aggregate = (
            len(aggregate_parameters) == 1
            and aggregate_parameters[0].group_by == expected_group_by
            and len(aggregate_parameters[0].measures) == 1
            and aggregate_parameters[0].measures[0].source == "metric.profit"
            and aggregate_parameters[0].measures[0].output == "profit"
            and aggregate_parameters[0].measures[0].function is AggregateFunction.SUM
        )
        if not exact_aggregate:
            diagnostics.append(
                self._error(
                    "COMPILATION_MODE_AGGREGATE_INVALID",
                    (
                        "Trusted modes require SUM(metric.profit) at the governed "
                        "region and period grain."
                    ),
                    path="nodes",
                    details={"mode": mode.value},
                )
            )
        output = node_by_id.get(sqg.output_node_id)
        if mode is CompilationMode.MONTHLY_REGIONAL_COMPARISON:
            pivot_parameters = [
                node.parameters
                for node in linear_path
                if isinstance(node.parameters, PivotParameters)
            ]
            value_bindings = (
                pivot_parameters[0].value_bindings if len(pivot_parameters) == 1 else []
            )
            actual_bindings = [
                (binding.alias, self._as_utc(binding.value)) for binding in value_bindings
            ]
            if len(time_windows) == 2:
                ordered_windows = sorted(time_windows, key=lambda window: window.start)
                expected_bindings = [
                    ("profit_current", self._as_utc(ordered_windows[1].start)),
                    ("profit_previous", self._as_utc(ordered_windows[0].start)),
                ]
            else:
                expected_bindings = [
                    ("profit_current", actual_bindings[0][1] if actual_bindings else None),
                    (
                        "profit_previous",
                        actual_bindings[1][1] if len(actual_bindings) > 1 else None,
                    ),
                ]
            exact_pivot = (
                len(pivot_parameters) == 1
                and pivot_parameters[0].index == ["commerce.sales_record.region"]
                and pivot_parameters[0].column == "commerce.sales_record.period"
                and pivot_parameters[0].value == "profit"
                and pivot_parameters[0].values == ["profit_current", "profit_previous"]
                and actual_bindings == expected_bindings
                and all(value is not None for _, value in actual_bindings)
                and (
                    len(actual_bindings) == 2
                    and actual_bindings[1][1] is not None
                    and actual_bindings[0][1] is not None
                    and actual_bindings[1][1] < actual_bindings[0][1]
                )
            )
            if not exact_pivot:
                diagnostics.append(
                    self._error(
                        "COMPILATION_MODE_PIVOT_INVALID",
                        "Monthly comparison requires the canonical governed period pivot.",
                        path="nodes",
                        details={"mode": mode.value},
                    )
                )
            project_mapping = (
                {column.alias: column.source for column in output.parameters.columns}
                if output is not None and isinstance(output.parameters, ProjectParameters)
                else {}
            )
            expected_project_mapping = {
                "region": "commerce.sales_record.region",
                "profit_current": "profit_current",
                "profit_previous": "profit_previous",
                "profit_change": "profit_change",
            }
            derive = (
                node_by_id.get(output.dependencies[0])
                if output is not None and len(output.dependencies) == 1
                else None
            )
            change_columns = (
                [column for column in derive.parameters.columns if column.output == "profit_change"]
                if derive is not None and isinstance(derive.parameters, DeriveParameters)
                else []
            )
            change_expression = change_columns[0].expression if len(change_columns) == 1 else None
            exact_change = (
                isinstance(change_expression, BinaryExpression)
                and change_expression.operator is BinaryOperator.SUBTRACT
                and isinstance(change_expression.left, ColumnExpression)
                and change_expression.left.column == "profit_current"
                and isinstance(change_expression.right, ColumnExpression)
                and change_expression.right.column == "profit_previous"
            )
            if project_mapping != expected_project_mapping or not exact_change:
                diagnostics.append(
                    self._error(
                        "COMPILATION_MODE_DERIVATION_INVALID",
                        "Monthly comparison requires profit_current minus profit_previous.",
                        path="nodes",
                        details={"mode": mode.value},
                    )
                )

    @classmethod
    def _runtime_value_matches(cls, value: object, data_type: ScalarType) -> bool:
        if data_type is ScalarType.STRING:
            return isinstance(value, str)
        if data_type is ScalarType.NUMBER:
            if isinstance(value, bool):
                return False
            if isinstance(value, int):
                return True
            return isinstance(value, float) and math.isfinite(value)
        if data_type is ScalarType.BOOLEAN:
            return isinstance(value, bool)
        return cls._parse_datetime(value) is not None

    @staticmethod
    def _as_utc(value: datetime) -> datetime | None:
        try:
            return value.astimezone(UTC)
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed

    @staticmethod
    def _ancestors(output_id: str, nodes: list[SQGNode]) -> set[str]:
        dependencies = {node.id: node.dependencies for node in nodes}
        visited: set[str] = set()
        pending = [output_id]
        while pending:
            node_id = pending.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            pending.extend(dependencies[node_id])
        return visited

    @staticmethod
    def _error(
        code: str,
        message: str,
        *,
        path: str | None = None,
        details: dict[str, str | int | float | bool | None] | None = None,
    ) -> Diagnostic:
        return Diagnostic(
            code=code,
            severity=DiagnosticSeverity.ERROR,
            stage=DiagnosticStage.VALIDATE,
            message=message,
            path=path,
            details=details or {},
        )
