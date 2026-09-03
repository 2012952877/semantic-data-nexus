from __future__ import annotations

from dataclasses import dataclass

from semantic_core.models import (
    AggregateNode,
    DeriveNode,
    FieldOperand,
    JoinNode,
    Ontology,
    PivotNode,
    ProjectNode,
    SelectNode,
    SemanticQueryGraph,
    SortNode,
    FilterNode,
    aggregation_output_type,
)


class SemanticValidationError(ValueError):
    """Raised when a structurally valid SQG violates ontology semantics."""


@dataclass(frozen=True)
class DirectBinding:
    entity: str
    field: str
    nullable: bool


def _matches_source(binding: DirectBinding, source: tuple[str, str]) -> bool:
    return (binding.entity, binding.field) == source


def _with_effective_nullability(
    bindings: set[DirectBinding], nullable: bool
) -> set[DirectBinding]:
    return {
        DirectBinding(
            entity=binding.entity,
            field=binding.field,
            nullable=binding.nullable or nullable,
        )
        for binding in bindings
    }


def validate_sqg(sqg: SemanticQueryGraph, ontology: Ontology) -> None:
    if sqg.ontology_version != ontology.version:
        raise SemanticValidationError(
            f"SQG ontology_version {sqg.ontology_version!r} does not match "
            f"ontology version {ontology.version!r}"
        )

    entities = {
        entity.id: {field.id: field for field in entity.fields}
        for entity in ontology.entities
    }
    metrics = {metric.id: metric for metric in ontology.metrics}
    relations = {relation.id: relation for relation in ontology.relations}
    node_entities: dict[str, set[str]] = {}
    node_lineage: dict[str, dict[str, set[tuple[str, str]]]] = {}
    node_bindings: dict[str, dict[str, set[DirectBinding]]] = {}

    for node in sqg.nodes:
        if isinstance(node, SelectNode):
            if node.params.entity not in entities:
                raise SemanticValidationError(
                    f"node {node.id!r} references unknown entity {node.params.entity!r}"
                )
            for field in node.params.fields:
                if field not in entities[node.params.entity]:
                    raise SemanticValidationError(
                        f"node {node.id!r} references unknown field "
                        f"{node.params.entity}.{field}"
                    )
            for output in node.outputs:
                expected = entities[node.params.entity][output.name]
                if output.data_type != expected.data_type:
                    raise SemanticValidationError(
                        f"node {node.id!r} output {output.name!r} has type "
                        f"{output.data_type.value!r}, expected {expected.data_type.value!r} "
                        "from the ontology"
                    )
                if output.nullable != expected.nullable:
                    raise SemanticValidationError(
                        f"node {node.id!r} output {output.name!r} has nullable="
                        f"{output.nullable}, expected nullable={expected.nullable} "
                        "from the ontology"
                    )
            node_entities[node.id] = {node.params.entity}
            direct = {
                output.name: {
                    DirectBinding(
                        entity=node.params.entity,
                        field=output.name,
                        nullable=output.nullable,
                    )
                }
                for output in node.outputs
            }
            node_lineage[node.id] = {
                output.name: {(node.params.entity, output.name)}
                for output in node.outputs
            }
            node_bindings[node.id] = direct
            continue

        inherited_entities = set().union(
            *(node_entities[dependency] for dependency in node.inputs)
        )
        input_lineage = [node_lineage[dependency] for dependency in node.inputs]
        input_bindings = [node_bindings[dependency] for dependency in node.inputs]
        if isinstance(node, AggregateNode):
            for measure in node.params.measures:
                metric = metrics.get(measure.metric)
                if metric is None:
                    raise SemanticValidationError(
                        f"node {node.id!r} references unknown metric {measure.metric!r}"
                    )
                if metric.entity not in inherited_entities:
                    raise SemanticValidationError(
                        f"metric {metric.id!r} belongs to entity {metric.entity!r}, "
                        f"not the node's input entities {sorted(inherited_entities)!r}"
                    )
                source = (metric.entity, metric.field)
                source_bindings = [
                    binding
                    for binding in input_bindings[0][measure.source]
                    if _matches_source(binding, source)
                ]
                if not source_bindings:
                    raise SemanticValidationError(
                        f"metric {metric.id!r} requires source field "
                        f"{metric.entity}.{metric.field}, "
                        f"which is not bound by input alias {measure.source!r} "
                        f"on node {node.id!r}"
                    )
                if len(source_bindings) != 1:
                    raise SemanticValidationError(
                        f"metric {metric.id!r} input alias {measure.source!r} "
                        "does not resolve to exactly one direct field occurrence"
                    )
                output = next(
                    output for output in node.outputs if output.name == measure.name
                )
                expected_type = aggregation_output_type(
                    metric.aggregation,
                    entities[metric.entity][metric.field].data_type,
                )
                if output.data_type != expected_type:
                    raise SemanticValidationError(
                        f"metric {metric.id!r} output {output.name!r} has type "
                        f"{output.data_type.value!r}, expected {expected_type.value!r}"
                    )
                source_nullable = any(
                    binding.nullable for binding in source_bindings
                )
                expected_nullable = (
                    False
                    if metric.aggregation.value in {"COUNT", "COUNT_DISTINCT"}
                    else source_nullable or not node.params.group_by
                )
                if output.nullable != expected_nullable:
                    raise SemanticValidationError(
                        f"metric {metric.id!r} output {output.name!r} has nullable="
                        f"{output.nullable}, expected nullable={expected_nullable}"
                    )
        if isinstance(node, JoinNode):
            relation = relations.get(node.params.relation)
            if relation is None:
                raise SemanticValidationError(
                    f"node {node.id!r} references unknown relation {node.params.relation!r}"
                )
            left_entities = node_entities[node.inputs[0]]
            right_entities = node_entities[node.inputs[1]]
            orientations: list[
                tuple[tuple[str, str], tuple[str, str]]
            ] = []
            if (
                relation.left_entity in left_entities
                and relation.right_entity in right_entities
            ):
                orientations.append(
                    (
                        (relation.left_entity, relation.left_field),
                        (relation.right_entity, relation.right_field),
                    )
                )
            if (
                relation.right_entity in left_entities
                and relation.left_entity in right_entities
            ):
                orientations.append(
                    (
                        (relation.right_entity, relation.right_field),
                        (relation.left_entity, relation.left_field),
                    )
                )
            if not orientations:
                raise SemanticValidationError(
                    f"relation {relation.id!r} does not connect join inputs "
                    f"{sorted(left_entities)!r} and {sorted(right_entities)!r}"
                )
            matching_orientations = [
                orientation
                for orientation in orientations
                if any(
                    _matches_source(binding, orientation[0])
                    for bindings in input_bindings[0].values()
                    for binding in bindings
                )
                and any(
                    _matches_source(binding, orientation[1])
                    for bindings in input_bindings[1].values()
                    for binding in bindings
                )
            ]
            if not matching_orientations:
                required = [
                    f"{left[0]}.{left[1]} -> {right[0]}.{right[1]}"
                    for left, right in orientations
                ]
                raise SemanticValidationError(
                    f"relation {relation.id!r} requires one direct join-key "
                    f"orientation to remain available: {required!r}"
                )
        node_entities[node.id] = inherited_entities
        if isinstance(node, (FilterNode, SortNode, ProjectNode)):
            node_lineage[node.id] = {
                output.name: input_lineage[0][output.name] for output in node.outputs
            }
            node_bindings[node.id] = {
                output.name: _with_effective_nullability(
                    input_bindings[0][output.name], output.nullable
                )
                for output in node.outputs
            }
        elif isinstance(node, DeriveNode):
            lineage = {
                output.name: input_lineage[0][output.name]
                for output in node.outputs
                if output.name in input_lineage[0]
            }
            bindings = {
                output.name: _with_effective_nullability(
                    input_bindings[0][output.name], output.nullable
                )
                for output in node.outputs
                if output.name in input_bindings[0]
            }
            for expression in node.params.expressions:
                lineage[expression.name] = set().union(
                    *(
                        input_lineage[0][operand.field]
                        for operand in (expression.left, expression.right)
                        if isinstance(operand, FieldOperand)
                    )
                )
                bindings[expression.name] = set()
            node_lineage[node.id] = lineage
            node_bindings[node.id] = bindings
        elif isinstance(node, AggregateNode):
            lineage = {
                field: input_lineage[0][field] for field in node.params.group_by
            }
            bindings = {
                field: _with_effective_nullability(
                    input_bindings[0][field],
                    next(output.nullable for output in node.outputs if output.name == field),
                )
                for field in node.params.group_by
            }
            lineage.update(
                {
                    measure.name: {(metrics[measure.metric].entity, metrics[measure.metric].field)}
                    for measure in node.params.measures
                }
            )
            bindings.update({measure.name: set() for measure in node.params.measures})
            node_lineage[node.id] = lineage
            node_bindings[node.id] = bindings
        elif isinstance(node, PivotNode):
            value_lineage = (
                input_lineage[0][node.params.value]
                | input_lineage[0][node.params.column]
            )
            node_lineage[node.id] = {
                **{
                    field: input_lineage[0][field] for field in node.params.index
                },
                **{
                    field: value_lineage
                    for field in node.params.expected_columns
                },
            }
            node_bindings[node.id] = {
                **{
                    field: input_bindings[0][field] for field in node.params.index
                },
                **{field: set() for field in node.params.expected_columns},
            }
        elif isinstance(node, JoinNode):
            node_lineage[node.id] = {
                projection.name: input_lineage[
                    0 if projection.source == "left" else 1
                ][projection.field]
                for projection in node.params.fields
            }
            outputs = {output.name: output for output in node.outputs}
            node_bindings[node.id] = {
                projection.name: _with_effective_nullability(
                    input_bindings[0 if projection.source == "left" else 1][
                        projection.field
                    ],
                    outputs[projection.name].nullable,
                )
                for projection in node.params.fields
            }
        else:
            raise AssertionError(f"unhandled SQG node type: {type(node).__name__}")
