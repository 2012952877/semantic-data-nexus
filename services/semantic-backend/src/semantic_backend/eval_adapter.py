from __future__ import annotations

import hashlib
import json
from typing import Any

from query_runtime.domain import OperatorKind, PhysicalPlan
from semantic_api.models import DiagnosticSeverity, QueryPolicy

from semantic_backend.service import IntegratedRunArtifact

_OPERATOR_NAMES = {"SELECT": "SCAN"}


def candidate_from_run(case_id: str, artifact: IntegratedRunArtifact) -> dict[str, Any]:
    detail = artifact.detail
    response = artifact.compile_response
    if detail.result is None or detail.manifest is None:
        raise ValueError("evaluation candidates require a committed integrated result")
    if response.normalized_sqg is None:
        raise ValueError("evaluation candidates require a normalized SQG")

    terms = [term.model_dump(mode="json") for term in response.resolved_terms]
    fields = sorted({str(term["machine_id"]) for term in terms if term.get("kind") == "field"})
    metrics = sorted({str(term["machine_id"]) for term in terms if term.get("kind") == "metric"})
    used_entities = {
        field.entity_id for field in response.selected_semantic_context.fields if field.id in fields
    } | {
        metric.entity_id
        for metric in response.selected_semantic_context.metrics
        if metric.id in metrics
    }
    entities = sorted(used_entities)

    time_range: dict[str, str] = {}
    period_filter = next(
        (
            item
            for item in detail.sqg.filters
            if item.field == "commerce.sales_record.period" and item.operator == "between"
        ),
        None,
    )
    if period_filter is not None:
        value = _json_object(period_filter.value)
        if isinstance(value, dict):
            time_range = {
                "start": str(value.get("start", "")),
                "end_exclusive": str(value.get("end_exclusive", "")),
                "source": "validated_sqg_filter",
            }

    plan, grain, order_by = _physical_plan(artifact.physical_plan)
    policies = [
        concept.query_policy
        for collection in (
            response.selected_semantic_context.entities,
            response.selected_semantic_context.fields,
            response.selected_semantic_context.metrics,
            response.selected_semantic_context.relations,
        )
        for concept in collection
    ]
    has_error = any(
        diagnostic.severity is DiagnosticSeverity.ERROR for diagnostic in response.diagnostics
    ) or any(diagnostic.severity.value == "error" for diagnostic in detail.diagnostics)
    allowed = not has_error and all(policy is QueryPolicy.ALLOW for policy in policies)
    behavior = (
        {"class": "success"}
        if detail.result is not None and allowed
        else {
            "class": "failed",
            "diagnostic_code": (
                detail.diagnostics[0].code if detail.diagnostics else "POLICY_DENIED"
            ),
        }
    )
    governance = {"decision": "allow" if allowed else "deny"}

    has_runtime_lineage = (
        bool(detail.lineage.nodes)
        and any(node.kind.value == "source" for node in detail.lineage.nodes)
        and any(edge.relation.value == "produces" for edge in detail.lineage.edges)
    )
    lineage_entities = entities if has_runtime_lineage else []
    lineage_fields = fields if has_runtime_lineage else []
    lineage_nodes = [{"id": node.id, "kind": node.kind.value} for node in detail.lineage.nodes]
    lineage_edges = [
        {
            "source": edge.source,
            "target": edge.target,
            "relation": edge.relation.value,
        }
        for edge in detail.lineage.edges
    ]
    topology = _normalized_lineage(lineage_nodes, lineage_edges)

    return {
        "id": case_id,
        "semantic": {
            "entities": entities,
            "fields": fields,
            "metrics": metrics,
            "relations": [],
        },
        "time_range": time_range,
        "member_normalization": {
            member: source_value
            for member, source_value in (artifact.member_normalization or {}).items()
        },
        "plan": plan,
        "result": {
            "schema": [
                {"name": column.key, "type": column.data_type.value}
                for column in detail.result.columns
            ],
            "grain": grain,
            "order_by": order_by,
            "rows": [
                {
                    column.key: value
                    for column, value in zip(detail.result.columns, row, strict=True)
                }
                for row in detail.result.rows
            ],
            "tolerance": 0.01,
        },
        "behavior": behavior,
        "governance": governance,
        "lineage": {
            "entities": lineage_entities,
            "fields": lineage_fields,
            "artifact": detail.manifest.checksum,
            "runtime_node_count": len(detail.lineage.nodes),
            "runtime_edge_count": len(detail.lineage.edges),
            "connector_provenance": list(artifact.connector_provenance),
            "relations": sorted({edge["relation"] for edge in lineage_edges}),
            "edge_semantics_valid": _lineage_edges_valid(
                lineage_nodes,
                lineage_edges,
            ),
            "topology": topology,
            "topology_sha256": hashlib.sha256(
                json.dumps(
                    topology,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        },
        "diagnostics": [
            {
                "code": diagnostic.code,
                "severity": diagnostic.severity.value,
                "scope": diagnostic.scope.value,
            }
            for diagnostic in detail.diagnostics
        ],
    }


def candidate_bundle(cases: dict[str, IntegratedRunArtifact]) -> dict[str, Any]:
    return {
        "artifact_version": "candidate-v0",
        "cases": {
            case_id: candidate_from_run(case_id, detail)
            for case_id, detail in sorted(cases.items())
        },
    }


def _physical_plan(
    plan: PhysicalPlan,
) -> tuple[dict[str, Any], list[str], list[str]]:
    nodes: list[dict[str, Any]] = []
    tails: dict[str, str] = {}
    grain: list[str] = []
    order_by: list[str] = []
    for physical in plan.nodes:
        references = physical.logical_operations
        if not references:
            raise ValueError("physical plan nodes require logical operation metadata")
        previous: str | None = None
        for index, reference in enumerate(references):
            node_id = f"{physical.id}:{reference.logical_node_id}"
            dependencies = (
                [previous]
                if previous is not None
                else [tails[dependency] for dependency in physical.dependencies]
            )
            operator = _OPERATOR_NAMES.get(
                reference.operation.value,
                reference.operation.value,
            )
            nodes.append(
                {
                    "id": node_id,
                    "operator": operator,
                    "depends_on": dependencies,
                    "inputs": [] if not dependencies else ["rows"],
                    "outputs": ["rows"],
                }
            )
            previous = node_id
            operation = _physical_operation(physical, index)
            if operation.kind is OperatorKind.AGGREGATE:
                grain = list(operation.group_by)
            elif operation.kind is OperatorKind.PIVOT:
                grain = list(operation.pivot_index)
            elif operation.kind is OperatorKind.SORT:
                order_by = [f"{item.column} {item.direction.value}" for item in operation.sort]
        assert previous is not None
        tails[physical.id] = previous
    edges = [[dependency, node["id"]] for node in nodes for dependency in node["depends_on"]]
    return (
        {
            "operators": [node["operator"] for node in nodes],
            "edges": edges,
            "nodes": nodes,
        },
        grain,
        order_by,
    )


def _physical_operation(physical: Any, index: int) -> Any:
    if physical.source_fragment is not None:
        return physical.source_fragment.operations[index]
    if index != 0 or physical.operator is None:
        raise ValueError("physical operator metadata is inconsistent")
    return physical.operator


def _json_object(value: str) -> Any:
    return json.loads(value)


def _lineage_edges_valid(
    nodes: list[dict[str, str]],
    edges: list[dict[str, str]],
) -> bool:
    kinds = {node["id"]: node["kind"] for node in nodes}
    expected = {
        "realized_as": ("logical", "physical"),
        "reads_from": ("physical", "source"),
        "depends_on": ("physical", "physical"),
        "produces": ("physical", "result"),
    }
    return all(
        edge["source"] in kinds
        and edge["target"] in kinds
        and edge["relation"] in expected
        and (kinds[edge["source"]], kinds[edge["target"]]) == expected[edge["relation"]]
        for edge in edges
    )


def _normalized_lineage(
    nodes: list[dict[str, str]],
    edges: list[dict[str, str]],
) -> dict[str, list[dict[str, str]]]:
    result_sources = {
        edge["target"]: edge["source"] for edge in edges if edge["relation"] == "produces"
    }
    identifiers = {
        node["id"]: (
            "result:" + result_sources[node["id"]].removeprefix("physical:")
            if node["kind"] == "result" and node["id"] in result_sources
            else node["id"]
        )
        for node in nodes
    }
    normalized_nodes = sorted(
        ({"id": identifiers[node["id"]], "kind": node["kind"]} for node in nodes),
        key=lambda item: (item["kind"], item["id"]),
    )
    normalized_edges = sorted(
        (
            {
                "source": identifiers[edge["source"]],
                "target": identifiers[edge["target"]],
                "relation": edge["relation"],
            }
            for edge in edges
        ),
        key=lambda item: (item["relation"], item["source"], item["target"]),
    )
    return {"nodes": normalized_nodes, "edges": normalized_edges}
