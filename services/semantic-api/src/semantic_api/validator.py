from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from semantic_api.models import (
    SQG,
    AggregateParameters,
    BinaryExpression,
    ColumnExpression,
    DeriveParameters,
    Diagnostic,
    DiagnosticSeverity,
    DiagnosticStage,
    FilterParameters,
    JoinParameters,
    LiteralExpression,
    Operator,
    PivotParameters,
    ProjectParameters,
    QueryPolicy,
    ResultColumn,
    ScalarType,
    SelectParameters,
    SemanticContext,
    SortParameters,
    SQGNode,
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
    columns: dict[str, ScalarType]
    grain: set[str]


class SQGValidator:
    def __init__(self, registry: OntologyRegistry) -> None:
        self.registry = registry

    def validate(
        self, raw_candidate: Mapping[str, Any], context: SemanticContext
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

        normalized: SQG | None = None
        if sqg.output_node_id in flows:
            output_flow = flows[sqg.output_node_id]
            inferred_schema = [
                ResultColumn(name=name, data_type=data_type)
                for name, data_type in output_flow.columns.items()
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
            self._require_column(
                node.parameters.predicate.column,
                source,
                f"{path}.parameters.predicate.column",
                diagnostics,
            )
            self._validate_member_predicate(node.parameters, path, diagnostics)
            return _Flow(dict(source.columns), set(source.grain))
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
            for key_index, key in enumerate(node.parameters.keys):
                self._require_column(
                    key.column,
                    source,
                    f"{path}.parameters.keys.{key_index}.column",
                    diagnostics,
                )
            return _Flow(dict(source.columns), set(source.grain))
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

    def _validate_member_predicate(
        self,
        parameters: FilterParameters,
        path: str,
        diagnostics: list[Diagnostic],
    ) -> None:
        field = self.registry.typed_fields().get(parameters.predicate.column)
        if field is None or not field.members:
            return
        raw_value = parameters.predicate.value
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        for value_index, value in enumerate(values):
            value_path = f"{path}.parameters.predicate.value"
            if isinstance(raw_value, list):
                value_path = f"{value_path}.{value_index}"
            if not isinstance(value, str) or not self.registry.has_member(value):
                diagnostics.append(
                    self._error(
                        "ONTOLOGY_MEMBER_UNKNOWN",
                        "Member predicates require an exact ontology member ID.",
                        path=value_path,
                    )
                )
                continue
            member_field_id, member = self.registry.members[value]
            if member_field_id != field.id:
                diagnostics.append(
                    self._error(
                        "MEMBER_FIELD_MISMATCH",
                        "Ontology member does not belong to the filtered field.",
                        path=value_path,
                        details={"member_id": value},
                    )
                )
            elif not member.enabled:
                diagnostics.append(
                    self._error(
                        "ONTOLOGY_CONCEPT_FORBIDDEN",
                        "Ontology member is disabled.",
                        path=value_path,
                        details={"concept_id": value},
                    )
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
        columns: dict[str, ScalarType] = {}
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
                columns[column] = field.data_type
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
                columns[column] = metric.data_type
            else:
                diagnostics.append(
                    self._error(
                        "ONTOLOGY_CONCEPT_UNKNOWN",
                        "Selected column is not an exact ontology field or metric ID.",
                        path=column_path,
                        details={"concept_id": column},
                    )
                )
        return _Flow(columns, grain)

    def _aggregate_flow(
        self,
        parameters: AggregateParameters,
        source: _Flow,
        path: str,
        diagnostics: list[Diagnostic],
    ) -> _Flow:
        columns: dict[str, ScalarType] = {}
        for group_index, group in enumerate(parameters.group_by):
            data_type = self._require_column(
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
            elif data_type is not None:
                columns[group] = data_type
        for measure_index, measure in enumerate(parameters.measures):
            measure_path = f"{path}.parameters.measures.{measure_index}"
            source_type = self._require_column(
                measure.source, source, f"{measure_path}.source", diagnostics
            )
            if source_type is not None and source_type is not ScalarType.NUMBER:
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
                columns[measure.output] = ScalarType.NUMBER
        return _Flow(columns, set(parameters.group_by))

    def _pivot_flow(
        self,
        parameters: PivotParameters,
        source: _Flow,
        path: str,
        diagnostics: list[Diagnostic],
    ) -> _Flow:
        columns: dict[str, ScalarType] = {}
        for index_position, column in enumerate(parameters.index):
            data_type = self._require_column(
                column, source, f"{path}.parameters.index.{index_position}", diagnostics
            )
            if data_type is not None:
                columns[column] = data_type
        self._require_column(parameters.column, source, f"{path}.parameters.column", diagnostics)
        value_type = self._require_column(
            parameters.value, source, f"{path}.parameters.value", diagnostics
        )
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
                columns[value] = value_type or ScalarType.NUMBER
        if len(set(parameters.values)) != len(parameters.values):
            diagnostics.append(
                self._error(
                    "DUPLICATE_OUTPUT_COLUMN",
                    "Pivot values must produce unique columns.",
                    path=f"{path}.parameters.values",
                )
            )
        return _Flow(columns, set(parameters.index))

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
            expression_type = self._expression_type(
                derived.expression, source, f"{column_path}.expression", diagnostics
            )
            if expression_type is not None and expression_type is not derived.data_type:
                diagnostics.append(
                    self._error(
                        "DERIVE_TYPE_MISMATCH",
                        "Derived output type does not match its expression.",
                        path=f"{column_path}.data_type",
                    )
                )
            columns[derived.output] = derived.data_type
        return _Flow(columns, set(source.grain))

    def _project_flow(
        self,
        parameters: ProjectParameters,
        source: _Flow,
        path: str,
        diagnostics: list[Diagnostic],
    ) -> _Flow:
        columns: dict[str, ScalarType] = {}
        grain: set[str] = set()
        for projection_index, projection in enumerate(parameters.columns):
            projection_path = f"{path}.parameters.columns.{projection_index}"
            data_type = self._require_column(
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
            elif data_type is not None:
                columns[projection.alias] = data_type
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
        return _Flow(columns, grain)

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
        self._require_column(
            parameters.left_key, inputs[0], f"{path}.parameters.left_key", diagnostics
        )
        self._require_column(
            parameters.right_key, inputs[1], f"{path}.parameters.right_key", diagnostics
        )
        columns = dict(inputs[0].columns)
        for column, data_type in inputs[1].columns.items():
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
                columns[column] = data_type
        return _Flow(columns, inputs[0].grain | inputs[1].grain)

    def _expression_type(
        self,
        expression: ColumnExpression | LiteralExpression | BinaryExpression,
        source: _Flow,
        path: str,
        diagnostics: list[Diagnostic],
    ) -> ScalarType | None:
        if isinstance(expression, ColumnExpression):
            return self._require_column(expression.column, source, f"{path}.column", diagnostics)
        if isinstance(expression, LiteralExpression):
            return expression.data_type
        left_type = self._expression_type(expression.left, source, f"{path}.left", diagnostics)
        right_type = self._expression_type(expression.right, source, f"{path}.right", diagnostics)
        if left_type is not None and left_type is not ScalarType.NUMBER:
            diagnostics.append(
                self._error(
                    "EXPRESSION_TYPE_INVALID",
                    "Binary arithmetic requires numeric operands.",
                    path=f"{path}.left",
                )
            )
        if right_type is not None and right_type is not ScalarType.NUMBER:
            diagnostics.append(
                self._error(
                    "EXPRESSION_TYPE_INVALID",
                    "Binary arithmetic requires numeric operands.",
                    path=f"{path}.right",
                )
            )
        return ScalarType.NUMBER

    def _validate_concept(
        self,
        machine_id: str,
        expected_kind: str,
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
        state = self.registry.concept_state(machine_id)
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
    ) -> ScalarType | None:
        data_type = flow.columns.get(column)
        if data_type is None:
            diagnostics.append(
                self._error(
                    "COLUMN_NOT_AVAILABLE",
                    "Operator references a column not produced by its dependency.",
                    path=path,
                    details={"column": column},
                )
            )
        return data_type

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
