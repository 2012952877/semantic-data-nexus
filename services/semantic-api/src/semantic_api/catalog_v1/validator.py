from __future__ import annotations

from dataclasses import dataclass, field

from semantic_api.catalog_v1.catalog import value_matches
from semantic_api.catalog_v1.models import (
    SQGV1,
    Aggregate,
    Comparison,
    CompilerContext,
    CompilerFailure,
    Filter,
    Join,
    Limit,
    MemberPredicate,
    Predicate,
    Project,
    ResultColumn,
    Scalar,
    Select,
    Sort,
    TimePredicate,
)

# These are forms with a typed adapter and existing runtime implementation.
CORE_CAPABILITIES = (
    "SELECT:fields",
    "FILTER:comparison",
    "FILTER:members",
    "FILTER:time",
    "JOIN:many_to_one",
    "JOIN:one_to_one",
    "AGGREGATE:metric",
    "PROJECT:columns",
    "SORT:keys",
    "LIMIT:count",
)


@dataclass
class Shape:
    columns: dict[str, Scalar]
    entities: set[str]
    pending: list[Predicate] = field(default_factory=list)
    aggregated: bool = False
    projected: bool = False
    duplicated_entities: set[str] = field(default_factory=set)


def validate(graph: SQGV1, context: CompilerContext) -> SQGV1:
    def require(condition: bool, code: str) -> None:
        if not condition:
            raise CompilerFailure(code)

    require(graph.catalog == context.catalog, "CATALOG_PIN_MISMATCH")
    require(not context.ambiguities, "CLARIFICATION_REQUIRED")
    catalog = context.semantic_catalog
    entities = {e.id: e for e in catalog.entities}
    fields = {f.id: f for f in catalog.fields}
    metrics = {m.id: m for m in catalog.metrics}
    relations = {r.id: r for r in catalog.relations}
    windows = {w.id: w for w in catalog.time_windows}
    caps = set(context.capabilities)
    shapes: dict[str, Shape] = {}
    used: set[str] = set()
    consumed: dict[str, int] = {}
    selected: set[str] = set()
    resolved_members: dict[str, set[str]] = {}
    resolved_windows = {r.choice.target_id for r in context.resolutions if r.choice.kind == "time"}
    resolved_fields = {r.choice.target_id for r in context.resolutions if r.choice.kind == "field"}
    for resolution in context.resolutions:
        choice = resolution.choice
        if choice.kind == "member":
            assert choice.field_id is not None
            resolved_members.setdefault(choice.field_id, set()).add(choice.target_id)

    def check_predicate(predicate: Predicate, shape: Shape) -> None:
        column = fields.get(predicate.column)
        require(column is not None and predicate.column in shape.columns, "FIELD_NOT_AVAILABLE")
        assert column is not None
        require(column.filterable, "FILTER_POLICY_DENIED")
        require("FILTER:" + predicate.kind in caps, "CAPABILITY_MISSING")
        if isinstance(predicate, Comparison):
            require(
                not column.members and column.data_type != "datetime", "GOVERNED_PREDICATE_REQUIRED"
            )
            require(value_matches(predicate.value, column.data_type), "PREDICATE_TYPE")
            require(
                column.data_type != "boolean" or predicate.operator in ("eq", "ne"),
                "PREDICATE_TYPE",
            )
        elif isinstance(predicate, MemberPredicate):
            allowed = {m.id for m in column.members}
            requested = set(predicate.member_ids)
            require(
                len(requested) == len(predicate.member_ids) and requested <= allowed,
                "MEMBER_NOT_AVAILABLE",
            )
            if predicate not in entities[column.entity_id].required_filters:
                require(requested == resolved_members.get(column.id, set()), "MEMBER_CONSTRAINT")
            used.update(requested)
        elif isinstance(predicate, TimePredicate):
            require(
                column.data_type == "datetime" and predicate.window_id in windows,
                "TIME_NOT_AVAILABLE",
            )
            if predicate not in entities[column.entity_id].required_filters:
                require(predicate.window_id in resolved_windows, "TIME_CONSTRAINT")
            used.add(predicate.window_id)
        used.add(column.id)

    for node in graph.nodes:
        require(node.id not in shapes, "GRAPH_DUPLICATE_NODE")
        require(len(set(node.dependencies)) == len(node.dependencies), "GRAPH_DUPLICATE_EDGE")
        require(all(dep in shapes for dep in node.dependencies), "GRAPH_DEPENDENCY")
        for dep in node.dependencies:
            consumed[dep] = consumed.get(dep, 0) + 1
            require(consumed[dep] == 1, "GRAPH_BRANCH_REUSE")
        op = node.operation
        if isinstance(op, Select):
            require("SELECT:fields" in caps, "CAPABILITY_MISSING")
            require(not node.dependencies and op.entity_id in entities, "ENTITY_NOT_AVAILABLE")
            require(op.entity_id not in selected, "ENTITY_RESELECTED")
            selected.add(op.entity_id)
            require(len(set(op.columns)) == len(op.columns), "DUPLICATE_COLUMN")
            require(
                all(c in fields and fields[c].entity_id == op.entity_id for c in op.columns),
                "FIELD_NOT_AVAILABLE",
            )
            pending = list(entities[op.entity_id].required_filters)
            pending.extend(
                MemberPredicate(kind="members", column=c, member_ids=tuple(sorted(ids)))
                for c, ids in resolved_members.items()
                if fields[c].entity_id == op.entity_id
            )
            if resolved_windows:
                time_fields = [
                    f.id
                    for f in fields.values()
                    if f.entity_id == op.entity_id and f.data_type == "datetime"
                ]
                explicit = [c for c in time_fields if c in resolved_fields]
                has_explicit_time = any(
                    f.id in resolved_fields and f.data_type == "datetime" for f in fields.values()
                )
                targets = explicit if has_explicit_time else time_fields
                require(len(targets) <= 1, "TIME_FIELD_AMBIGUOUS")
                if targets:
                    pending.extend(
                        TimePredicate(kind="time", column=targets[0], window_id=w)
                        for w in sorted(resolved_windows)
                    )
            require(all(p.column in op.columns for p in pending), "REQUIRED_FILTER_COLUMN")
            shape = Shape({c: fields[c].data_type for c in op.columns}, {op.entity_id}, pending)
            for predicate in pending:
                check_predicate(predicate, shape)
            used.update((op.entity_id, *op.columns))
        else:
            expected = 2 if isinstance(op, Join) else 1
            require(len(node.dependencies) == expected, "GRAPH_ARITY")
            inputs = [shapes[d] for d in node.dependencies]
            source = inputs[0]
            shape = Shape(
                dict(source.columns),
                set(source.entities),
                list(source.pending),
                source.aggregated,
                source.projected,
                set(source.duplicated_entities),
            )
            if isinstance(op, Filter):
                require(not shape.aggregated and not shape.projected, "FILTER_ORDER")
                check_predicate(op.predicate, shape)
                shape.pending = [p for p in shape.pending if p != op.predicate]
            else:
                require(all(not item.pending for item in inputs), "REQUIRED_FILTER_MISSING")
                if isinstance(op, Join):
                    relation = relations.get(op.relation_id)
                    require(relation is not None, "RELATION_NOT_AVAILABLE")
                    assert relation is not None
                    require("JOIN:" + relation.cardinality in caps, "CAPABILITY_MISSING")
                    right = inputs[1]
                    require(not any(s.aggregated or s.projected for s in inputs), "JOIN_ORDER")
                    require(
                        relation.from_entity in source.entities
                        and right.entities == {relation.to_entity}
                        and not source.entities & right.entities,
                        "JOIN_CARDINALITY",
                    )
                    require(
                        relation.from_field in source.columns
                        and relation.to_field in right.columns,
                        "JOIN_KEY_MISSING",
                    )
                    require(
                        not source.columns.keys() & right.columns.keys(), "JOIN_COLUMN_COLLISION"
                    )
                    shape.columns.update(right.columns)
                    shape.entities.update(right.entities)
                    if relation.cardinality == "many_to_one":
                        shape.duplicated_entities.update(right.entities)
                    used.add(relation.id)
                elif isinstance(op, Aggregate):
                    require("AGGREGATE:metric" in caps, "CAPABILITY_MISSING")
                    require(not shape.aggregated and not shape.projected, "AGGREGATION_ORDER")
                    require(len(set(op.group_by)) == len(op.group_by), "DUPLICATE_COLUMN")
                    require(
                        all(
                            c in shape.columns and c in fields and fields[c].groupable
                            for c in op.group_by
                        ),
                        "GROUP_POLICY_DENIED",
                    )
                    outputs: dict[str, Scalar] = {c: shape.columns[c] for c in op.group_by}
                    for measure in op.measures:
                        metric = metrics.get(measure.metric_id)
                        require(metric is not None, "METRIC_NOT_AVAILABLE")
                        assert metric is not None
                        require(
                            metric.entity_id not in shape.duplicated_entities, "METRIC_JOIN_FANOUT"
                        )
                        require(metric.field_id in shape.columns, "METRIC_FIELD_MISSING")
                        require(
                            measure.output not in outputs and measure.output not in fields,
                            "AGGREGATE_OUTPUT_COLLISION",
                        )
                        outputs[measure.output] = (
                            "integer"
                            if metric.function == "count"
                            else "number"
                            if metric.function == "avg"
                            else fields[metric.field_id].data_type
                        )
                        used.add(metric.id)
                    shape.columns = outputs
                    shape.aggregated = True
                elif isinstance(op, Project):
                    require("PROJECT:columns" in caps, "CAPABILITY_MISSING")
                    require(not shape.projected, "PROJECT_ORDER")
                    require(all(p.source in shape.columns for p in op.columns), "OUTPUT_FIELD")
                    require(
                        len({p.alias.casefold() for p in op.columns}) == len(op.columns),
                        "OUTPUT_DUPLICATE",
                    )
                    shape.columns = {p.alias: shape.columns[p.source] for p in op.columns}
                    shape.projected = True
                elif isinstance(op, Sort):
                    require("SORT:keys" in caps, "CAPABILITY_MISSING")
                    require(all(k.column in shape.columns for k in op.keys), "SORT_FIELD")
                elif isinstance(op, Limit):
                    require("LIMIT:count" in caps, "CAPABILITY_MISSING")
                    require(shape.projected, "LIMIT_ORDER")
        shapes[node.id] = shape
    require(graph.output_node_id == graph.nodes[-1].id, "OUTPUT_NODE")
    require(set(shapes) - set(consumed) == {graph.output_node_id}, "GRAPH_DISCONNECTED")
    result = shapes[graph.output_node_id]
    require(not result.pending and result.projected, "OUTPUT_NOT_PROJECTED")
    require(
        tuple(ResultColumn(name=name, data_type=kind) for name, kind in result.columns.items())
        == graph.result_schema,
        "RESULT_SCHEMA",
    )
    required = {r.choice.target_id for r in context.resolutions}
    require(required <= used, "RESOLVED_CONSTRAINT_MISSING")
    return graph
