from __future__ import annotations

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
    node_provenance: dict[str, dict[str, set[tuple[str, str]]]] = {}

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
            node_provenance[node.id] = {
                output.name: {(node.params.entity, output.name)}
                for output in node.outputs
            }
            continue

        inherited_entities = set().union(
            *(node_entities[dependency] for dependency in node.inputs)
        )
        input_provenance = [node_provenance[dependency] for dependency in node.inputs]
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
                if not any(
                    source in provenance
                    for provenance in input_provenance[0].values()
                ):
                    raise SemanticValidationError(
                        f"metric {metric.id!r} requires source field "
                        f"{metric.entity}.{metric.field}, "
                        f"which is not available to node {node.id!r}"
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
                source_nullable = entities[metric.entity][metric.field].nullable
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
            if (
                relation.left_entity in left_entities
                and relation.right_entity in right_entities
            ):
                left_key, right_key = relation.left_field, relation.right_field
            elif (
                relation.right_entity in left_entities
                and relation.left_entity in right_entities
            ):
                left_key, right_key = relation.right_field, relation.left_field
            else:
                raise SemanticValidationError(
                    f"relation {relation.id!r} does not connect join inputs "
                    f"{sorted(left_entities)!r} and {sorted(right_entities)!r}"
                )
            left_source = next(
                entity
                for entity in (relation.left_entity, relation.right_entity)
                if entity in left_entities
            )
            right_source = (
                relation.right_entity
                if left_source == relation.left_entity
                else relation.left_entity
            )
            if not any(
                (left_source, left_key) in provenance
                for provenance in input_provenance[0].values()
            ) or not any(
                (right_source, right_key) in provenance
                for provenance in input_provenance[1].values()
            ):
                raise SemanticValidationError(
                    f"relation {relation.id!r} requires join keys "
                    f"{left_key!r} and {right_key!r} to remain available"
                )
        node_entities[node.id] = inherited_entities
        if isinstance(node, (FilterNode, SortNode, ProjectNode)):
            node_provenance[node.id] = {
                output.name: input_provenance[0][output.name] for output in node.outputs
            }
        elif isinstance(node, DeriveNode):
            provenance = {
                output.name: input_provenance[0][output.name]
                for output in node.outputs
                if output.name in input_provenance[0]
            }
            for expression in node.params.expressions:
                provenance[expression.name] = set().union(
                    *(
                        input_provenance[0][operand.field]
                        for operand in (expression.left, expression.right)
                        if isinstance(operand, FieldOperand)
                    )
                )
            node_provenance[node.id] = provenance
        elif isinstance(node, AggregateNode):
            provenance = {
                field: input_provenance[0][field] for field in node.params.group_by
            }
            provenance.update(
                {
                    measure.name: {(metrics[measure.metric].entity, metrics[measure.metric].field)}
                    for measure in node.params.measures
                }
            )
            node_provenance[node.id] = provenance
        elif isinstance(node, PivotNode):
            value_provenance = (
                input_provenance[0][node.params.value]
                | input_provenance[0][node.params.column]
            )
            node_provenance[node.id] = {
                **{
                    field: input_provenance[0][field] for field in node.params.index
                },
                **{
                    field: value_provenance
                    for field in node.params.expected_columns
                },
            }
        elif isinstance(node, JoinNode):
            node_provenance[node.id] = {
                projection.name: input_provenance[
                    0 if projection.source == "left" else 1
                ][projection.field]
                for projection in node.params.fields
            }
        else:
            raise AssertionError(f"unhandled SQG node type: {type(node).__name__}")
