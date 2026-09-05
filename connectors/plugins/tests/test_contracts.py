from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError
from query_runtime.domain import (
    OperatorKind,
    OperatorSpec,
    OperatorSpecV1,
    PhysicalNode,
    PhysicalNodeKind,
    PhysicalPlan,
    SortSpec,
)
from query_runtime.errors import PlanFailure
from query_runtime.planner import (
    CapabilityPlanner,
    ExactConceptBinder,
    LogicalNode,
    ValidatedLogicalGraph,
)

from nexus_plugins.contracts import CredentialReference, PluginDescriptor
from nexus_plugins.export_contracts import schemas


def test_v0_shape_and_unknown_extensions() -> None:
    old = OperatorSpec(kind=OperatorKind.SORT, sort=(SortSpec(column="id"),))
    assert set(old.model_dump()) == {
        "kind",
        "columns",
        "predicate",
        "group_by",
        "aggregates",
        "expressions",
        "sort",
        "limit",
        "join_type",
        "join_keys",
        "pivot_index",
        "pivot_column",
        "pivot_value",
        "pivot_values",
        "time_grain",
    }
    assert OperatorSpec.model_validate_json(old.model_dump_json()) == old
    with pytest.raises(ValidationError):
        OperatorSpec.model_validate({"kind": "WINDOW", "window": {"function": "row_number"}})
    with pytest.raises(ValidationError):
        OperatorSpec(kind=OperatorKind.UNION_ALL)
    with pytest.raises(ValidationError):
        OperatorSpec.model_validate({"kind": "SORT", "partition_by": ["id"]})


def test_v1_roundtrip_and_version_mismatch() -> None:
    spec = OperatorSpecV1(version="query-runtime/v1", kind=OperatorKind.DISTINCT, columns=("id",))
    node = LogicalNode(id="distinct", dependencies=("source",), operation=spec)
    with pytest.raises(ValidationError, match="v1"):
        ValidatedLogicalGraph(id="graph", nodes=(node,), output_node_id="distinct")
    graph = ValidatedLogicalGraph(
        version="query-runtime/v1", id="graph", nodes=(node,), output_node_id="distinct"
    )
    assert ValidatedLogicalGraph.model_validate_json(graph.model_dump_json()) == graph
    physical = PhysicalNode(
        id="distinct",
        kind=PhysicalNodeKind.OPERATOR,
        operation=spec.kind,
        operator=spec,
        wave=1,
        dependencies=("source",),
        logical_node_ids=("distinct",),
    )
    with pytest.raises(ValidationError, match="v1"):
        PhysicalPlan(id="plan", nodes=(physical,), output_node_id="distinct")
    plan = PhysicalPlan(
        version="query-runtime/v1",
        id="plan",
        nodes=(physical,),
        output_node_id="distinct",
    )
    assert PhysicalPlan.model_validate_json(plan.model_dump_json()) == plan
    for version in (None, "query-runtime/v0", "query-runtime/v2"):
        payload = spec.model_dump()
        payload["version"] = version
        with pytest.raises(ValidationError):
            OperatorSpecV1.model_validate(payload)


@pytest.mark.parametrize("kind", ["ASK", "ACT", "SEARCH"])
def test_governed_backend_required_before_execution(kind: str) -> None:
    graph = ValidatedLogicalGraph(
        version="query-runtime/v1",
        id="blocked",
        output_node_id="blocked",
        nodes=(
            LogicalNode(
                id="blocked", operation=OperatorSpecV1(version="query-runtime/v1", kind=kind)
            ),
        ),
    )
    with pytest.raises(PlanFailure, match="governed backend"):
        CapabilityPlanner(
            binder=ExactConceptBinder({}),
            sources={},
            capabilities={},
        ).plan(graph)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "SAMPLE", "sample_seed": True, "limit": 1},
        {"kind": "SAMPLE", "sample_seed": -1, "limit": 1},
        {"kind": "DATE"},
        {"kind": "DISTINCT", "date": {"column": "d", "output": "x", "grain": "day"}},
        {"kind": "WINDOW", "window": {"function": "sum", "column": "x", "output": "s"}},
        {
            "kind": "WINDOW",
            "sort": [{"column": "id"}],
            "window": {"function": "row_number", "output": "n", "preceding": 2},
        },
        {"kind": "SELECT", "sql": "synthetic forbidden payload"},
        {"kind": "ACT", "tool_payload": {"execute": True}},
    ],
)
def test_invalid_forms_fail_closed(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OperatorSpecV1.model_validate({"version": "query-runtime/v1", **payload})


def test_credential_references_not_secrets_or_paths() -> None:
    reference = CredentialReference(id="credential:synthetic-reader")
    descriptor = PluginDescriptor(
        version="nexus-plugins/v1",
        id="postgresql",
        kind="resolver",
        runtime_versions=("query-runtime/v1",),
        interchange=("arrow",),
        credential_ref=reference,
    )
    assert PluginDescriptor.model_validate_json(descriptor.model_dump_json()) == descriptor
    for value in ("postgresql://example.invalid/db", "../token", "password=synthetic"):
        with pytest.raises(ValidationError):
            CredentialReference(id=value)
    with pytest.raises(ValidationError):
        PluginDescriptor.model_validate(
            {**descriptor.model_dump(), "password": "synthetic-forbidden"}
        )


def test_capability_matrix_covers_exactly_26_families() -> None:
    root = Path(__file__).resolve().parents[3]
    matrix = json.loads((root / "contracts/plugins/v1/operator-capabilities.json").read_text())
    expected = set(OperatorKind) - {OperatorKind.SOURCE, OperatorKind.LIMIT}
    assert len(expected) == 26
    assert {item["family"] for item in matrix["operators"]} == expected
    assert len(matrix["operators"]) == 26
    for item in matrix["operators"]:
        assert item["forms"] and item["limits"] and item["negative_cases"]
        assert item["status"] == (
            "unsupported" if item["family"] in {"ASK", "ACT", "SEARCH"} else "implemented"
        )


def test_schema_snapshots_and_examples() -> None:
    root = Path(__file__).resolve().parents[3] / "contracts/plugins/v1"
    for name, schema in schemas().items():
        assert json.loads((root / name).read_text()) == schema
        jsonschema.Draft202012Validator.check_schema(schema)
    examples = json.loads((root / "examples.json").read_text())
    models = {
        "plugin": PluginDescriptor,
        "operator": OperatorSpecV1,
        "logical-graph": ValidatedLogicalGraph,
    }
    for example in examples:
        jsonschema.validate(example["value"], schemas()[example["contract"] + ".schema.json"])
        models[example["contract"]].model_validate(example["value"])
