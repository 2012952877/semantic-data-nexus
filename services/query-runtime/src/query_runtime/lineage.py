"""Safe lineage recording for logical, physical, source, and result artifacts."""

from __future__ import annotations

from query_runtime.domain import (
    CommittedManifest,
    LineageEdge,
    LineageGraph,
    LineageNode,
    PhysicalNode,
    PhysicalNodeKind,
    PhysicalPlan,
)


class LineageRecorder:
    def __init__(self, run_id: str, plan: PhysicalPlan) -> None:
        self.run_id = run_id
        self._nodes: dict[str, LineageNode] = {}
        self._edges: set[tuple[str, str, str]] = set()
        self._physical = {node.id: node for node in plan.nodes}
        for node in plan.nodes:
            self._record_plan_node(node)

    def _record_plan_node(self, node: PhysicalNode) -> None:
        physical_id = f"physical:{node.id}"
        parameters: tuple[dict[str, str], ...] = ()
        source_alias = None
        source_type = None
        if node.kind is PhysicalNodeKind.SOURCE_FRAGMENT:
            assert node.source_fragment is not None
            source = node.source_fragment.source
            source_id = f"source:{source.alias}"
            self._nodes[source_id] = LineageNode(
                id=source_id,
                kind="source",
                source_alias=source.alias,
                source_type=source.source_type,
            )
            parameters = tuple(
                parameter.safe_metadata()
                for parameter in node.source_fragment.parameters
            )
            source_alias = source.alias
            source_type = source.source_type
            self._edge(source_id, physical_id, "reads_from")
        self._nodes[physical_id] = LineageNode(
            id=physical_id,
            kind="physical",
            operation=node.operation.value,
            source_alias=source_alias,
            source_type=source_type,
            parameter_metadata=parameters,
        )
        for logical_id in node.logical_node_ids:
            lineage_id = f"logical:{logical_id}"
            self._nodes[lineage_id] = LineageNode(
                id=lineage_id, kind="logical", operation=node.operation.value
            )
            self._edge(lineage_id, physical_id, "realized_as")
        for dependency in node.dependencies:
            self._edge(f"physical:{dependency}", physical_id, "depends_on")

    def record_result(self, node_id: str, manifest: CommittedManifest) -> None:
        result_id = f"result:{manifest.result.result_id}"
        self._nodes[result_id] = LineageNode(
            id=result_id,
            kind="result",
            result_id=manifest.result.result_id,
        )
        self._edge(f"physical:{node_id}", result_id, "produces")

    def graph(self) -> LineageGraph:
        nodes = tuple(self._nodes[key] for key in sorted(self._nodes))
        edges = tuple(
            LineageEdge(source=source, target=target, relation=relation)  # type: ignore[arg-type]
            for source, target, relation in sorted(self._edges)
        )
        return LineageGraph(run_id=self.run_id, nodes=nodes, edges=edges)

    def _edge(self, source: str, target: str, relation: str) -> None:
        self._edges.add((source, target, relation))
