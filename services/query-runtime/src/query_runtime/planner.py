"""Capability-aware M0 binder and physical planner."""

from __future__ import annotations

from collections import defaultdict
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from query_runtime.domain import (
    DOMAIN_VERSION,
    BoundColumn,
    BoundSource,
    CapabilityCatalog,
    OperatorKind,
    OperatorSpec,
    PhysicalNode,
    PhysicalNodeKind,
    PhysicalPlan,
    SourceFragment,
)
from query_runtime.errors import BindingFailure, PlanFailure


class LogicalNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    operation: OperatorSpec
    dependencies: tuple[str, ...] = ()
    source_alias: str | None = None
    concepts: tuple[str, ...] = ()
    estimated_rows: int | None = None
    estimated_bytes: int | None = None
    requires_spill: bool = False


class ValidatedLogicalGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = DOMAIN_VERSION
    id: str
    nodes: tuple[LogicalNode, ...]
    output_node_id: str


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
    unknown = sorted(
        {dep for node in nodes for dep in node.dependencies if dep not in by_id}
    )
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
    ) -> None:
        self._binder = binder
        self._sources = sources
        self._capabilities = capabilities

    def plan(self, graph: ValidatedLogicalGraph) -> PhysicalPlan:
        ordered = topological_order(graph.nodes)
        for node in ordered:
            for concept in node.concepts:
                self._binder.bind(concept)

        physical: dict[str, PhysicalNode] = {}
        mapping: dict[str, str] = {}
        for logical in ordered:
            dependency_ids = tuple(dict.fromkeys(mapping[dep] for dep in logical.dependencies))
            capability = self._capabilities.get(logical.source_alias or "")
            pushdown = (
                logical.source_alias is not None
                and logical.operation.kind not in LOCAL_ONLY
                and capability is not None
                and self._supports(logical, capability)
            )
            node_id = f"physical-{logical.id}"
            if pushdown:
                source = self._sources.get(logical.source_alias or "")
                if source is None:
                    raise PlanFailure(
                        "PLAN_SOURCE_MISSING",
                        f"Source alias '{logical.source_alias}' is not bound",
                    )
                predecessor = (
                    physical[dependency_ids[0]]
                    if len(dependency_ids) == 1
                    and dependency_ids[0] in physical
                    and physical[dependency_ids[0]].kind
                    is PhysicalNodeKind.SOURCE_FRAGMENT
                    else None
                )
                can_fuse = (
                    predecessor is not None
                    and predecessor.source_fragment is not None
                    and predecessor.source_fragment.source.alias == source.alias
                )
                if can_fuse:
                    assert predecessor is not None
                    assert predecessor.source_fragment is not None
                    operations = (
                        *predecessor.source_fragment.operations,
                        logical.operation,
                    )
                    fused_logical_ids = (*predecessor.logical_node_ids, logical.id)
                    physical_dependencies = predecessor.dependencies
                else:
                    operations = (logical.operation,)
                    fused_logical_ids = (logical.id,)
                    physical_dependencies = dependency_ids
                physical_node = PhysicalNode(
                    id=node_id,
                    kind=PhysicalNodeKind.SOURCE_FRAGMENT,
                    operation=logical.operation.kind,
                    dependencies=physical_dependencies,
                    wave=0,
                    logical_node_ids=fused_logical_ids,
                    source_fragment=SourceFragment(
                        source=source, operations=operations
                    ),
                )
            else:
                physical_node = PhysicalNode(
                    id=node_id,
                    kind=PhysicalNodeKind.OPERATOR,
                    operation=logical.operation.kind,
                    dependencies=dependency_ids,
                    wave=0,
                    logical_node_ids=(logical.id,),
                    operator=logical.operation,
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
            id=f"physical-{graph.id}",
            nodes=planned,
            output_node_id=output_id,
        )

    @staticmethod
    def _supports(logical: LogicalNode, capability: CapabilityCatalog) -> bool:
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
