from __future__ import annotations

from typing import Any

from semantic_backend.service import IntegratedRunArtifact

_OPERATOR_NAMES = {"SELECT": "SCAN"}


def candidate_from_run(case_id: str, artifact: IntegratedRunArtifact) -> dict[str, Any]:
    detail = artifact.detail
    if detail.result is None:
        raise ValueError("evaluation candidates require a succeeded integrated run")

    response = artifact.compile_response
    if response.normalized_sqg is None:
        raise ValueError("evaluation candidates require a normalized SQG")
    sqg = response.normalized_sqg
    terms = [term.model_dump(mode="json") for term in response.resolved_terms]
    fields = sorted({str(term["machine_id"]) for term in terms if term.get("kind") == "field"})
    metrics = sorted({str(term["machine_id"]) for term in terms if term.get("kind") == "metric"})
    entities = sorted({str(term["machine_id"]) for term in terms if term.get("kind") == "entity"})
    if (fields or metrics) and "commerce.sales_record" not in entities:
        entities.append("commerce.sales_record")
        entities.sort()

    operators = [
        _OPERATOR_NAMES.get(node.operator.value, node.operator.value) for node in sqg.nodes
    ]
    nodes = [
        {
            "id": node.id,
            "operator": _OPERATOR_NAMES.get(node.operator.value, node.operator.value),
            "depends_on": list(node.dependencies),
            "inputs": [] if not node.dependencies else ["rows"],
            "outputs": ["rows"],
        }
        for node in sqg.nodes
    ]
    edges = [[dependency, node.id] for node in sqg.nodes for dependency in node.dependencies]
    filter_node = next(
        (node for node in sqg.nodes if node.operator.value == "FILTER"),
        None,
    )
    time_range: dict[str, str] = {}
    if filter_node is not None:
        predicate = filter_node.parameters.model_dump(mode="json").get("predicate", {})
        value = predicate.get("value", {})
        if isinstance(value, dict):
            time_range = {
                "start": str(value.get("start", "")),
                "end_exclusive": str(value.get("end_exclusive", "")),
                "source": "validated_sqg_filter",
            }

    aggregate = next(node for node in sqg.nodes if node.operator.value == "AGGREGATE")
    grain = list(aggregate.parameters.model_dump(mode="json").get("group_by", []))
    pivot = next((node for node in sqg.nodes if node.operator.value == "PIVOT"), None)
    if pivot is not None:
        grain = list(pivot.parameters.model_dump(mode="json").get("index", []))
    sort = next((node for node in sqg.nodes if node.operator.value == "SORT"), None)
    order_by = []
    if sort is not None:
        order_by = [
            f"{item['column']} {item['direction']}"
            for item in sort.parameters.model_dump(mode="json").get("keys", [])
        ]

    return {
        "id": case_id,
        "semantic": {
            "entities": entities,
            "fields": fields,
            "metrics": metrics,
            "relations": [],
        },
        "time_range": time_range,
        "member_normalization": {},
        "plan": {
            "operators": operators,
            "edges": edges,
            "nodes": nodes,
        },
        "result": {
            "schema": [
                {"name": column.name, "type": column.data_type.value}
                for column in sqg.result_schema
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
        "behavior": {"class": "success"},
        "governance": {"decision": "allow"},
        "lineage": {
            "entities": entities,
            "fields": fields,
            "artifact": f"integrated:{case_id}",
        },
    }


def candidate_bundle(cases: dict[str, IntegratedRunArtifact]) -> dict[str, Any]:
    return {
        "artifact_version": "candidate-v0",
        "cases": {
            case_id: candidate_from_run(case_id, detail)
            for case_id, detail in sorted(cases.items())
        },
    }
