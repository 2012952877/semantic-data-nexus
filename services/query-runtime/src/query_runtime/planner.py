"""Capability-aware M0 binder and physical planner."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from query_runtime.domain import (
    BLOCKED_OPERATOR_KINDS,
    DOMAIN_VERSION,
    BoundColumn,
    BoundSource,
    CapabilityCatalog,
    ExpressionKind,
    LogicalOperationRef,
    OperatorKind,
    OperatorSpec,
    OperatorSpecV1,
    PhysicalNode,
    PhysicalNodeKind,
    PhysicalPlan,
    PlanVersion,
    SourceFragment,
    TypedExpression,
)
from query_runtime.errors import BindingFailure, PlanFailure


class LogicalNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    operation: OperatorSpecV1 | OperatorSpec
    dependencies: tuple[str, ...] = ()
    source_alias: str | None = None
    concepts: tuple[str, ...] = ()
    estimated_rows: int | None = None
    estimated_bytes: int | None = None
    requires_spill: bool = False


class ValidatedLogicalGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: PlanVersion = DOMAIN_VERSION
    id: str
    nodes: tuple[LogicalNode, ...]
    output_node_id: str

    @model_validator(mode="after")
    def version_boundary(self) -> ValidatedLogicalGraph:
        if self.version == DOMAIN_VERSION and any(
            isinstance(node.operation, OperatorSpecV1) for node in self.nodes
        ):
            raise ValueError("v1 operators require a query-runtime/v1 graph")
        return self


class ConceptBinder(Protocol):
    def bind(self, concept: str) -> BoundColumn: ...


class ExactConceptBinder:
    """Small adapter boundary for ontology/source bindings."""

    def __init__(self, bindings: dict[str, tuple[BoundColumn, ...]]) -> None:
        self._bindings = bindings

    def bind(self, concept: str) -> BoundColumn:
        candidates = self._bindings.get(concept, ())
        if not candidates:
            raise BindingFailure(
                "BINDING_MISSING",
                f"No exact source binding exists for concept '{concept}'",
                details={"concept": concept},
            )
        if len(candidates) > 1:
            raise BindingFailure(
                "BINDING_AMBIGUOUS",
                f"Concept '{concept}' has multiple exact source bindings",
                details={"concept": concept, "candidate_count": len(candidates)},
            )
        return candidates[0]


LOCAL_ONLY = frozenset({OperatorKind.PIVOT, OperatorKind.DERIVE, OperatorKind.PROJECT})


def topological_order(nodes: tuple[LogicalNode, ...]) -> tuple[LogicalNode, ...]:
    by_id = {node.id: node for node in nodes}
    if len(by_id) != len(nodes):
        raise PlanFailure("PLAN_DUPLICATE_NODE", "Logical graph contains duplicate node IDs")
    unknown = sorted({dep for node in nodes for dep in node.dependencies if dep not in by_id})
    if unknown:
        raise PlanFailure(
            "PLAN_MISSING_DEPENDENCY",
            "Logical graph references missing dependencies",
            details={"dependencies": unknown},
        )
    indegree = {node.id: len(node.dependencies) for node in nodes}
    dependents: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for dep in node.dependencies:
            dependents[dep].append(node.id)
    ready = sorted(node_id for node_id, count in indegree.items() if count == 0)
    ordered: list[LogicalNode] = []
    while ready:
        node_id = ready.pop(0)
        ordered.append(by_id[node_id])
        for child in sorted(dependents[node_id]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(ordered) != len(nodes):
        raise PlanFailure("PLAN_CYCLE", "Logical graph contains a dependency cycle")
    return tuple(ordered)


class CapabilityPlanner:
    """Deterministic heuristic planner; deliberately not a cost optimizer."""

    def __init__(
        self,
        *,
        binder: ConceptBinder,
        sources: dict[str, BoundSource],
        capabilities: dict[str, CapabilityCatalog],
        fragment_supported: Callable[[SourceFragment], bool] | None = None,
    ) -> None:
        self._binder = binder
        self._sources = sources
        self._capabilities = capabilities
        self._fragment_supported = fragment_supported

    def plan(self, graph: ValidatedLogicalGraph) -> PhysicalPlan:
        ordered = topological_order(graph.nodes)
        for node in ordered:
            if node.operation.kind in BLOCKED_OPERATOR_KINDS:
                raise PlanFailure(
                    "OPERATOR_UNSUPPORTED",
                    f"{node.operation.kind} requires a governed backend and is not executable",
                )
        bindings: dict[str, tuple[BoundColumn, ...]] = {}
        for node in ordered:
            bound = tuple(self._binder.bind(concept) for concept in node.concepts)
            if node.source_alias is not None:
                mismatched = sorted(
                    {
                        column.source_alias
                        for column in bound
                        if column.source_alias != node.source_alias
                    }
                )
                if mismatched:
                    raise BindingFailure(
                        "BINDING_SOURCE_MISMATCH",
                        f"Logical node '{node.id}' bindings do not match its source",
                        details={
                            "logical_source": node.source_alias,
                            "bound_sources": mismatched,
                        },
                    )
            bindings[node.id] = bound

        physical: dict[str, PhysicalNode] = {}
        mapping: dict[str, str] = {}
        for logical in ordered:
            operation = _apply_bindings(logical.operation, bindings[logical.id])
            dependency_ids = tuple(dict.fromkeys(mapping[dep] for dep in logical.dependencies))
            capability = self._capabilities.get(logical.source_alias or "")
            candidate_pushdown = (
                logical.source_alias is not None
                and operation.kind not in LOCAL_ONLY
                and not isinstance(operation, OperatorSpecV1)
                and capability is not None
                and self._supports(logical, capability)
            )
            node_id = f"physical-{logical.id}"
            source = self._sources.get(logical.source_alias or "")
            predecessor = (
                physical[dependency_ids[0]]
                if len(dependency_ids) == 1
                and dependency_ids[0] in physical
                and physical[dependency_ids[0]].kind is PhysicalNodeKind.SOURCE_FRAGMENT
                else None
            )
            can_fuse = (
                predecessor is not None
                and predecessor.source_fragment is not None
                and source is not None
                and predecessor.source_fragment.source.alias == source.alias
            )
            pushdown = candidate_pushdown and (not dependency_ids or can_fuse)
            if pushdown and source is not None and self._fragment_supported is not None:
                previous = (
                    predecessor.source_fragment.operations
                    if can_fuse
                    and predecessor is not None
                    and predecessor.source_fragment is not None
                    else ()
                )
                pushdown = self._fragment_supported(
                    SourceFragment(
                        source=source,
                        operations=(*previous, operation),
                    )
                )
            if pushdown:
                if source is None:
                    raise PlanFailure(
                        "PLAN_SOURCE_MISSING",
                        f"Source alias '{logical.source_alias}' is not bound",
                    )
                if can_fuse:
                    assert predecessor is not None
                    assert predecessor.source_fragment is not None
                    operations = (
                        *predecessor.source_fragment.operations,
                        operation,
                    )
                    fused_logical_ids = (*predecessor.logical_node_ids, logical.id)
                    logical_operations = (
                        *predecessor.logical_operations,
                        LogicalOperationRef(
                            logical_node_id=logical.id,
                            operation=operation.kind,
                        ),
                    )
                    physical_dependencies = predecessor.dependencies
                    bound_columns = _deduplicate_columns(
                        (*predecessor.source_fragment.bound_columns, *bindings[logical.id])
                    )
                else:
                    operations = (operation,)
                    fused_logical_ids = (logical.id,)
                    logical_operations = (
                        LogicalOperationRef(
                            logical_node_id=logical.id,
                            operation=operation.kind,
                        ),
                    )
                    physical_dependencies = dependency_ids
                    bound_columns = bindings[logical.id]
                physical_node = PhysicalNode(
                    id=node_id,
                    kind=PhysicalNodeKind.SOURCE_FRAGMENT,
                    operation=operation.kind,
                    dependencies=physical_dependencies,
                    wave=0,
                    logical_node_ids=fused_logical_ids,
                    logical_operations=logical_operations,
                    source_fragment=SourceFragment(
                        source=source,
                        operations=operations,
                        bound_columns=bound_columns,
                    ),
                )
            else:
                if not dependency_ids:
                    raise PlanFailure(
                        "PLAN_ROOT_LOCAL_UNSUPPORTED",
                        f"Root operation '{operation.kind}' cannot execute locally without input",
                    )
                physical_node = PhysicalNode(
                    id=node_id,
                    kind=PhysicalNodeKind.OPERATOR,
                    operation=operation.kind,
                    dependencies=dependency_ids,
                    wave=0,
                    logical_node_ids=(logical.id,),
                    logical_operations=(
                        LogicalOperationRef(
                            logical_node_id=logical.id,
                            operation=operation.kind,
                        ),
                    ),
                    operator=operation,
                )
            physical[node_id] = physical_node
            mapping[logical.id] = node_id

        if graph.output_node_id not in mapping:
            raise PlanFailure("PLAN_OUTPUT_MISSING", "Graph output node does not exist")
        output_id = mapping[graph.output_node_id]
        reachable = _reachable(output_id, physical)
        planned = _assign_waves(
            tuple(node for node_id, node in physical.items() if node_id in reachable)
        )
        return PhysicalPlan(
            version=graph.version,
            id=f"physical-{graph.id}",
            nodes=planned,
            output_node_id=output_id,
        )

    @staticmethod
    def _supports(logical: LogicalNode, capability: CapabilityCatalog) -> bool:
        if logical.operation.kind is OperatorKind.JOIN:
            return False
        if not capability.supports(logical.operation.kind):
            return False
        if logical.operation.kind is OperatorKind.AGGREGATE and any(
            item.function not in capability.aggregate_functions
            for item in logical.operation.aggregates
        ):
            return False
        if (
            logical.operation.time_grain is not None
            and logical.operation.time_grain not in capability.time_grains
        ):
            return False
        if logical.requires_spill and not capability.spill_supported:
            return False
        if (
            logical.estimated_rows is not None
            and capability.max_rows is not None
            and logical.estimated_rows > capability.max_rows
        ):
            return False
        return not (
            logical.estimated_bytes is not None
            and capability.max_bytes is not None
            and logical.estimated_bytes > capability.max_bytes
        )


def _deduplicate_columns(columns: tuple[BoundColumn, ...]) -> tuple[BoundColumn, ...]:
    unique: dict[tuple[str, str, str], BoundColumn] = {}
    for column in columns:
        key = (column.concept, column.source_alias, column.column_name)
        unique[key] = column
    return tuple(unique[key] for key in sorted(unique))


def _apply_bindings(operation: OperatorSpec, columns: tuple[BoundColumn, ...]) -> OperatorSpec:
    bindings = {column.concept: column for column in columns}

    def name(value: str) -> str:
        return bindings[value].column_name if value in bindings else value

    def expression(value: TypedExpression) -> TypedExpression:
        if value.kind is ExpressionKind.COLUMN and value.column in bindings:
            assert value.column is not None
            binding = bindings[value.column]
            if binding.data_type is not value.data_type:
                raise BindingFailure(
                    "BINDING_TYPE_MISMATCH",
                    f"Concept '{value.column}' type does not match its binding",
                    details={
                        "concept": value.column,
                        "logical_type": value.data_type.value,
                        "physical_type": binding.data_type.value,
                    },
                )
            return value.model_copy(update={"column": binding.column_name})
        if not value.args:
            return value
        return value.model_copy(
            update={"args": tuple(expression(argument) for argument in value.args)}
        )

    bound_operation = operation.model_copy(
        update={
            "columns": tuple(name(column) for column in operation.columns),
            "predicate": (
                operation.predicate.model_copy(
                    update={"expression": expression(operation.predicate.expression)}
                )
                if operation.predicate is not None
                else None
            ),
            "group_by": tuple(name(column) for column in operation.group_by),
            "aggregates": tuple(
                aggregate.model_copy(
                    update={
                        "expression": (
                            expression(aggregate.expression)
                            if aggregate.expression is not None
                            else None
                        )
                    }
                )
                for aggregate in operation.aggregates
            ),
            "expressions": tuple(
                item.model_copy(update={"expression": expression(item.expression)})
                for item in operation.expressions
            ),
            "sort": tuple(
                item.model_copy(update={"column": name(item.column)}) for item in operation.sort
            ),
            "join_keys": tuple(
                item.model_copy(update={"left": name(item.left), "right": name(item.right)})
                for item in operation.join_keys
            ),
            "pivot_index": tuple(name(column) for column in operation.pivot_index),
            "pivot_column": (
                name(operation.pivot_column) if operation.pivot_column is not None else None
            ),
            "pivot_value": (
                name(operation.pivot_value) if operation.pivot_value is not None else None
            ),
        }
    )
    if isinstance(bound_operation, OperatorSpecV1):
        changes: dict[str, object] = {
            "partition_by": tuple(name(column) for column in bound_operation.partition_by),
        }
        for field in ("window", "date", "explode", "impute"):
            payload = getattr(bound_operation, field)
            if payload is not None:
                update: dict[str, object] = {}
                if payload.column is not None:
                    update["column"] = name(payload.column)
                if field == "impute":
                    update["value"] = expression(payload.value)
                changes[field] = payload.model_copy(update=update)
        if bound_operation.unpivot is not None:
            changes["unpivot"] = bound_operation.unpivot.model_copy(
                update={
                    "columns": tuple(name(column) for column in bound_operation.unpivot.columns),
                }
            )
        return bound_operation.model_copy(update=changes)
    return bound_operation


def _reachable(output_id: str, nodes: dict[str, PhysicalNode]) -> set[str]:
    result: set[str] = set()
    pending = [output_id]
    while pending:
        node_id = pending.pop()
        if node_id in result:
            continue
        result.add(node_id)
        pending.extend(nodes[node_id].dependencies)
    return result


def _assign_waves(nodes: tuple[PhysicalNode, ...]) -> tuple[PhysicalNode, ...]:
    by_id = {node.id: node for node in nodes}
    remaining = set(by_id)
    waves: dict[str, int] = {}
    ordered: list[PhysicalNode] = []
    while remaining:
        ready = sorted(
            node_id
            for node_id in remaining
            if all(dependency in waves for dependency in by_id[node_id].dependencies)
        )
        if not ready:
            raise PlanFailure("PLAN_CYCLE", "Physical plan contains a dependency cycle")
        for node_id in ready:
            node = by_id[node_id]
            wave = (
                0
                if not node.dependencies
                else max(waves[dependency] for dependency in node.dependencies) + 1
            )
            waves[node_id] = wave
            ordered.append(node.model_copy(update={"wave": wave}))
            remaining.remove(node_id)
    return tuple(ordered)
