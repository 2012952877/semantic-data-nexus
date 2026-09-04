from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from pydantic import BaseModel, ConfigDict
from query_runtime.domain import (
    AggregateFunction,
    AggregateSpec,
    BoundColumn,
    BoundPredicate,
    BoundSource,
    CapabilityCatalog,
    ExpressionKind,
    JoinKey,
    JoinType,
    NamedExpression,
    OperatorKind,
    OperatorSpec,
    ScalarType,
    SortDirection,
    SortSpec,
    TypedExpression,
)
from query_runtime.planner import ExactConceptBinder, LogicalNode, ValidatedLogicalGraph
from semantic_api.models import (
    BinaryExpression,
    ColumnExpression,
    CompilationMode,
    CompileResponse,
    CompileStatus,
    DeriveParameters,
    FilterParameters,
    JoinParameters,
    LiteralExpression,
    Operator,
    PivotParameters,
    ProjectParameters,
    SelectParameters,
    SortParameters,
)
from semantic_api.models import (
    ScalarType as CompilerScalarType,
)


class AdapterFailure(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _SourceDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    alias: str
    source_type: str
    object_name: str


class _ConceptDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    column: str
    data_type: ScalarType
    mode_columns: dict[CompilationMode, str] = {}


class SourceMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str
    source: _SourceDefinition
    concepts: dict[str, _ConceptDefinition]
    members: dict[str, str]

    @classmethod
    def load_default(cls) -> SourceMapping:
        resource = files("semantic_backend.resources").joinpath("source_mapping.v0.json")
        return cls.model_validate(json.loads(resource.read_text(encoding="utf-8")))

    def column_for(self, concept: str, mode: CompilationMode) -> BoundColumn:
        definition = self.concepts.get(concept)
        if definition is None:
            raise AdapterFailure(
                "ADAPTER_CONCEPT_UNMAPPED",
                f"No reviewed source mapping exists for concept '{concept}'.",
            )
        return BoundColumn(
            concept=concept,
            source_alias=self.source.alias,
            column_name=definition.mode_columns.get(mode, definition.column),
            data_type=definition.data_type,
        )


@dataclass(frozen=True)
class AdaptedExecution:
    graph: ValidatedLogicalGraph
    binder: ExactConceptBinder
    sources: dict[str, BoundSource]
    capabilities: dict[str, CapabilityCatalog]
    metadata: dict[str, str]


_SCALAR_TYPES = {
    CompilerScalarType.STRING: ScalarType.STRING,
    CompilerScalarType.NUMBER: ScalarType.FLOAT,
    CompilerScalarType.BOOLEAN: ScalarType.BOOLEAN,
    CompilerScalarType.DATETIME: ScalarType.TIMESTAMP,
}

_BINARY_KINDS = {
    "add": ExpressionKind.ADD,
    "subtract": ExpressionKind.SUBTRACT,
    "multiply": ExpressionKind.MULTIPLY,
    "divide": ExpressionKind.DIVIDE,
}

_PREDICATE_KINDS = {
    "eq": ExpressionKind.EQUAL,
    "ne": ExpressionKind.NOT_EQUAL,
    "gt": ExpressionKind.GREATER_THAN,
    "gte": ExpressionKind.GREATER_EQUAL,
    "lt": ExpressionKind.LESS_THAN,
    "lte": ExpressionKind.LESS_EQUAL,
}


class CompilerRuntimeAdapter:
    def __init__(self, mapping: SourceMapping | None = None) -> None:
        self.mapping = mapping or SourceMapping.load_default()

    def adapt(
        self,
        response: CompileResponse,
        *,
        run_id: str,
        compilation_mode: CompilationMode,
        source_type: str | None = None,
    ) -> AdaptedExecution:
        if response.status is not CompileStatus.SUCCEEDED or response.normalized_sqg is None:
            raise AdapterFailure(
                "ADAPTER_COMPILE_NOT_SUCCEEDED",
                "Only a succeeded, normalized compiler response can be adapted.",
            )

        source = BoundSource(
            alias=self.mapping.source.alias,
            source_type=source_type or self.mapping.source.source_type,
            object_name=self.mapping.source.object_name,
        )
        referenced_concepts = {
            concept
            for node in response.normalized_sqg.nodes
            for concept in self._node_concepts(node.parameters)
        }
        used_concepts = {
            concept
            for concept in referenced_concepts
            if concept.startswith(("commerce.sales_record.", "metric."))
            and concept not in self.mapping.members
        }
        bindings: dict[str, tuple[BoundColumn, ...]] = {
            concept: (self.mapping.column_for(concept, compilation_mode),)
            for concept in sorted(used_concepts)
        }
        binder = ExactConceptBinder(bindings)
        capabilities = CapabilityCatalog(
            source_alias=source.alias,
            source_type=source.source_type,
            operator_kinds=frozenset(
                {
                    OperatorKind.SELECT,
                    OperatorKind.FILTER,
                    OperatorKind.AGGREGATE,
                    OperatorKind.SORT,
                }
            ),
            aggregate_functions=frozenset(AggregateFunction),
            max_rows=100_000,
            max_bytes=32 * 1024 * 1024,
        )

        column_types = {concept: binding[0].data_type for concept, binding in bindings.items()}
        pivot_aliases: dict[str, str] = {}
        logical_nodes: list[LogicalNode] = []
        for node in response.normalized_sqg.nodes:
            operation, produced_types, aliases = self._operation(
                node.operator,
                node.parameters,
                column_types,
                pivot_aliases,
            )
            column_types.update(produced_types)
            pivot_aliases.update(aliases)
            concepts = tuple(
                sorted(
                    concept
                    for concept in self._node_concepts(node.parameters)
                    if concept in bindings
                )
            )
            logical_nodes.append(
                LogicalNode(
                    id=node.id,
                    operation=operation,
                    dependencies=tuple(node.dependencies),
                    source_alias=source.alias,
                    concepts=concepts,
                    estimated_rows=10_000,
                    estimated_bytes=4 * 1024 * 1024,
                )
            )

        graph = ValidatedLogicalGraph(
            id=f"sqg-{run_id}",
            nodes=tuple(logical_nodes),
            output_node_id=response.normalized_sqg.output_node_id,
        )
        return AdaptedExecution(
            graph=graph,
            binder=binder,
            sources={source.alias: source},
            capabilities={source.alias: capabilities},
            metadata={
                "mapping_version": self.mapping.version,
                "ontology_version": response.selected_semantic_context.ontology_version,
                "compilation_mode": compilation_mode.value,
            },
        )

    def _operation(
        self,
        operator: Operator,
        parameters: Any,
        column_types: dict[str, ScalarType],
        pivot_aliases: dict[str, str],
    ) -> tuple[OperatorSpec, dict[str, ScalarType], dict[str, str]]:
        if operator is Operator.SELECT and isinstance(parameters, SelectParameters):
            return (
                OperatorSpec(kind=OperatorKind.SELECT, columns=tuple(parameters.columns)),
                {},
                {},
            )
        if operator is Operator.FILTER and isinstance(parameters, FilterParameters):
            predicate = self._predicate(
                parameters.predicate.column,
                parameters.predicate.operator.value,
                parameters.predicate.value,
                column_types,
            )
            return OperatorSpec(kind=OperatorKind.FILTER, predicate=predicate), {}, {}
        if operator is Operator.AGGREGATE:
            aggregates = tuple(
                AggregateSpec(
                    name=measure.output,
                    function=AggregateFunction(measure.function.value),
                    expression=TypedExpression.col(
                        measure.source,
                        self._type_of(measure.source, column_types),
                    ),
                )
                for measure in parameters.measures
            )
            produced = {
                item.name: item.expression.data_type for item in aggregates if item.expression
            }
            return (
                OperatorSpec(
                    kind=OperatorKind.AGGREGATE,
                    group_by=tuple(parameters.group_by),
                    aggregates=aggregates,
                ),
                produced,
                {},
            )
        if operator is Operator.PIVOT and isinstance(parameters, PivotParameters):
            aliases = {item.alias: item.value.isoformat() for item in parameters.value_bindings}
            if set(aliases) != set(parameters.values):
                raise AdapterFailure(
                    "ADAPTER_PIVOT_BINDING_INVALID",
                    "Every pivot output alias must have one reviewed bound value.",
                )
            value_type = self._type_of(parameters.value, column_types)
            produced = {value: value_type for value in aliases.values()}
            return (
                OperatorSpec(
                    kind=OperatorKind.PIVOT,
                    pivot_index=tuple(parameters.index),
                    pivot_column=parameters.column,
                    pivot_value=parameters.value,
                    pivot_values=tuple(aliases[value] for value in parameters.values),
                ),
                produced,
                aliases,
            )
        if operator is Operator.DERIVE and isinstance(parameters, DeriveParameters):
            expressions = tuple(
                NamedExpression(
                    name=column.output,
                    expression=self._expression(
                        column.expression,
                        column_types,
                        pivot_aliases,
                        _SCALAR_TYPES[column.data_type],
                    ),
                )
                for column in parameters.columns
            )
            return (
                OperatorSpec(kind=OperatorKind.DERIVE, expressions=expressions),
                {item.name: item.expression.data_type for item in expressions},
                {},
            )
        if operator is Operator.PROJECT and isinstance(parameters, ProjectParameters):
            expressions = tuple(
                NamedExpression(
                    name=column.alias,
                    expression=TypedExpression.col(
                        pivot_aliases.get(column.source, column.source),
                        self._type_of(
                            pivot_aliases.get(column.source, column.source),
                            column_types,
                        ),
                    ),
                )
                for column in parameters.columns
            )
            return (
                OperatorSpec(kind=OperatorKind.PROJECT, expressions=expressions),
                {item.name: item.expression.data_type for item in expressions},
                {},
            )
        if operator is Operator.SORT and isinstance(parameters, SortParameters):
            return (
                OperatorSpec(
                    kind=OperatorKind.SORT,
                    sort=tuple(
                        SortSpec(
                            column=pivot_aliases.get(item.column, item.column),
                            direction=SortDirection(item.direction.value),
                        )
                        for item in parameters.keys
                    ),
                ),
                {},
                {},
            )
        if operator is Operator.JOIN and isinstance(parameters, JoinParameters):
            return (
                OperatorSpec(
                    kind=OperatorKind.JOIN,
                    join_type=JoinType(parameters.join_type.value),
                    join_keys=(JoinKey(left=parameters.left_key, right=parameters.right_key),),
                ),
                {},
                {},
            )
        raise AdapterFailure(
            "ADAPTER_OPERATOR_UNSUPPORTED",
            f"The reviewed adapter does not support operator '{operator.value}'.",
        )

    def _predicate(
        self,
        column: str,
        operator: str,
        value: Any,
        column_types: dict[str, ScalarType],
    ) -> BoundPredicate:
        data_type = self._type_of(column, column_types)
        left = TypedExpression.col(column, data_type)
        if operator == "between":
            if not isinstance(value, dict) or set(value) != {"start", "end_exclusive"}:
                raise AdapterFailure(
                    "ADAPTER_PREDICATE_INVALID",
                    "A between predicate requires start and end_exclusive values.",
                )
            expression = TypedExpression(
                kind=ExpressionKind.AND,
                data_type=ScalarType.BOOLEAN,
                args=(
                    self._comparison(ExpressionKind.GREATER_EQUAL, left, value["start"]),
                    self._comparison(ExpressionKind.LESS_THAN, left, value["end_exclusive"]),
                ),
            )
        elif operator == "in":
            if not isinstance(value, list) or not value:
                raise AdapterFailure(
                    "ADAPTER_PREDICATE_INVALID",
                    "An in predicate requires at least one typed value.",
                )
            comparisons = tuple(
                self._comparison(ExpressionKind.EQUAL, left, item) for item in value
            )
            expression = comparisons[0]
            for comparison in comparisons[1:]:
                expression = TypedExpression(
                    kind=ExpressionKind.OR,
                    data_type=ScalarType.BOOLEAN,
                    args=(expression, comparison),
                )
        else:
            kind = _PREDICATE_KINDS.get(operator)
            if kind is None or isinstance(value, (list, dict)):
                raise AdapterFailure(
                    "ADAPTER_PREDICATE_UNSUPPORTED",
                    f"Predicate operator '{operator}' is not supported.",
                )
            expression = self._comparison(kind, left, value)
        return BoundPredicate(expression=expression)

    def _comparison(
        self,
        kind: ExpressionKind,
        left: TypedExpression,
        value: str | int | float | bool | None,
    ) -> TypedExpression:
        if left.column == "commerce.sales_record.region":
            if not isinstance(value, str) or value not in self.mapping.members:
                raise AdapterFailure(
                    "ADAPTER_MEMBER_UNMAPPED",
                    "No reviewed source value mapping exists for the governed region member.",
                )
            source_value: str | int | float | bool | None = self.mapping.members[value]
        else:
            source_value = value
        return TypedExpression(
            kind=kind,
            data_type=ScalarType.BOOLEAN,
            args=(left, TypedExpression.literal(source_value, left.data_type)),
        )

    def _expression(
        self,
        expression: ColumnExpression | LiteralExpression | BinaryExpression,
        column_types: dict[str, ScalarType],
        pivot_aliases: dict[str, str],
        output_type: ScalarType,
    ) -> TypedExpression:
        if isinstance(expression, ColumnExpression):
            column = pivot_aliases.get(expression.column, expression.column)
            return TypedExpression.col(column, self._type_of(column, column_types))
        if isinstance(expression, LiteralExpression):
            return TypedExpression.literal(expression.value, _SCALAR_TYPES[expression.data_type])
        return TypedExpression(
            kind=_BINARY_KINDS[expression.operator.value],
            data_type=output_type,
            args=(
                self._expression(expression.left, column_types, pivot_aliases, output_type),
                self._expression(expression.right, column_types, pivot_aliases, output_type),
            ),
        )

    @staticmethod
    def _type_of(column: str, column_types: dict[str, ScalarType]) -> ScalarType:
        try:
            return column_types[column]
        except KeyError as exc:
            raise AdapterFailure(
                "ADAPTER_COLUMN_UNMAPPED",
                f"No reviewed type mapping exists for column '{column}'.",
            ) from exc

    @staticmethod
    def _node_concepts(parameters: Any) -> set[str]:
        payload = parameters.model_dump(mode="python")
        concepts: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, str) and "." in value:
                concepts.add(value)
            elif isinstance(value, dict):
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    visit(nested)

        visit(payload)
        return concepts
