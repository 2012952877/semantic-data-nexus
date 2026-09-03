from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from semantic_api.models import SemanticContext
from semantic_api.ontology import OntologyRegistry
from semantic_api.provider import StaticFixtureProvider
from semantic_api.validator import SQGValidator


def codes(
    registry: OntologyRegistry,
    semantic_context: SemanticContext,
    candidate: dict[str, Any],
) -> set[str]:
    result = SQGValidator(registry).validate(candidate, semantic_context)
    return {item.code for item in result.diagnostics}


def test_validates_and_normalizes_golden_flows(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    validator = SQGValidator(registry)
    quarterly = validator.validate(StaticFixtureProvider._quarterly_profit([]), semantic_context)
    monthly = validator.validate(StaticFixtureProvider._monthly_comparison(), semantic_context)

    assert quarterly.valid
    assert monthly.valid
    assert [
        node.operator.value
        for node in monthly.normalized.nodes[1:]  # type: ignore[union-attr]
    ] == ["AGGREGATE", "PIVOT", "DERIVE", "PROJECT"]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda value: value["nodes"][1].update(id="select_sales"),
            "DUPLICATE_NODE_ID",
        ),
        (
            lambda value: value["nodes"][1].update(dependencies=["aggregate_profit"]),
            "SELF_DEPENDENCY",
        ),
        (
            lambda value: value["nodes"][1].update(dependencies=["sort_profit"]),
            "FORWARD_DEPENDENCY",
        ),
        (
            lambda value: value["nodes"][1].update(dependencies=["missing"]),
            "MISSING_DEPENDENCY",
        ),
        (
            lambda value: value["nodes"][1].update(dependencies=["select_sales", "select_sales"]),
            "DUPLICATE_DEPENDENCY",
        ),
        (
            lambda value: value.update(output_node_id="missing"),
            "OUTPUT_NODE_MISSING",
        ),
        (
            lambda value: value["nodes"].append(deepcopy(value["nodes"][0]) | {"id": "dead"}),
            "NODE_NOT_OUTPUT_REACHABLE",
        ),
        (
            lambda value: value["nodes"][0].update(operator="EXECUTE_SQL"),
            "SCHEMA_INVALID",
        ),
        (
            lambda value: value["nodes"][0]["parameters"].update(
                columns=["commerce.sales_record.unknown"]
            ),
            "ONTOLOGY_CONCEPT_UNKNOWN",
        ),
        (
            lambda value: value["nodes"][0]["parameters"].update(
                columns=["commerce.sales_record.note"]
            ),
            "ONTOLOGY_CONCEPT_FORBIDDEN",
        ),
        (
            lambda value: value["nodes"][0].update(operator="FILTER"),
            "OPERATOR_PARAMETER_MISMATCH",
        ),
        (
            lambda value: value["nodes"][-1]["parameters"]["columns"][0].update(source="invented"),
            "COLUMN_NOT_AVAILABLE",
        ),
        (
            lambda value: value.update(result_schema=[{"name": "wrong", "data_type": "string"}]),
            "RESULT_SCHEMA_MISMATCH",
        ),
        (
            lambda value: value["nodes"][1]["parameters"]["measures"][0].update(
                source="commerce.sales_record.region"
            ),
            "AGGREGATE_TYPE_INVALID",
        ),
    ],
)
def test_validation_classes(
    registry: OntologyRegistry,
    semantic_context: SemanticContext,
    mutation: Any,
    expected: str,
) -> None:
    candidate = StaticFixtureProvider._quarterly_profit([])
    mutation(candidate)

    assert expected in codes(registry, semantic_context, candidate)


def test_context_selection_is_enforced(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    candidate = StaticFixtureProvider._quarterly_profit([])
    limited_context = semantic_context.model_copy(update={"metrics": []})

    assert "CONTEXT_CONCEPT_NOT_SELECTED" in codes(registry, limited_context, candidate)


def test_filter_column_flow(registry: OntologyRegistry, semantic_context: SemanticContext) -> None:
    candidate = StaticFixtureProvider._quarterly_profit([])
    candidate["nodes"][1] = {
        "id": "aggregate_profit",
        "name": "Invalid filter",
        "operator": "FILTER",
        "dependencies": ["select_sales"],
        "parameters": {
            "kind": "FILTER",
            "predicate": {"column": "invented", "operator": "eq", "value": "x"},
        },
    }

    assert "COLUMN_NOT_AVAILABLE" in codes(registry, semantic_context, candidate)


def test_filter_requires_exact_member_id(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    candidate = StaticFixtureProvider._quarterly_profit([])
    candidate["nodes"].insert(
        1,
        {
            "id": "filter_member",
            "name": "Invalid member filter",
            "operator": "FILTER",
            "dependencies": ["select_sales"],
            "parameters": {
                "kind": "FILTER",
                "predicate": {
                    "column": "commerce.sales_record.region",
                    "operator": "eq",
                    "value": "East",
                },
            },
        },
    )
    candidate["nodes"][2]["dependencies"] = ["filter_member"]

    assert "ONTOLOGY_MEMBER_UNKNOWN" in codes(registry, semantic_context, candidate)


def test_sort_column_flow(registry: OntologyRegistry, semantic_context: SemanticContext) -> None:
    candidate = StaticFixtureProvider._quarterly_profit([])
    candidate["nodes"][2]["parameters"]["keys"][0]["column"] = "invented"

    assert "COLUMN_NOT_AVAILABLE" in codes(registry, semantic_context, candidate)


def test_derive_collision_and_missing_expression_column(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    candidate = StaticFixtureProvider._monthly_comparison()
    derive = candidate["nodes"][3]["parameters"]["columns"][0]
    derive["output"] = "profit_current"
    derive["expression"]["left"]["column"] = "invented"

    result_codes = codes(registry, semantic_context, candidate)
    assert "DERIVE_OUTPUT_COLLISION" in result_codes
    assert "COLUMN_NOT_AVAILABLE" in result_codes


def test_pivot_column_flow(registry: OntologyRegistry, semantic_context: SemanticContext) -> None:
    candidate = StaticFixtureProvider._monthly_comparison()
    candidate["nodes"][2]["parameters"]["value"] = "invented"

    assert "COLUMN_NOT_AVAILABLE" in codes(registry, semantic_context, candidate)


def test_projection_cannot_remove_all_grain(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    candidate = StaticFixtureProvider._quarterly_profit([])
    candidate["nodes"][-1]["parameters"]["columns"] = [{"source": "profit", "alias": "profit"}]
    candidate["result_schema"] = [{"name": "profit", "data_type": "number"}]

    assert "GRAIN_LOST" in codes(registry, semantic_context, candidate)


def test_join_policy_and_column_collision(registry: OntologyRegistry) -> None:
    candidate = {
        "schema_version": "sqg.v0",
        "nodes": [
            {
                "id": "left",
                "name": "Left",
                "operator": "SELECT",
                "dependencies": [],
                "parameters": {
                    "kind": "SELECT",
                    "entity_id": "commerce.sales_record",
                    "columns": ["commerce.sales_record.region"],
                },
            },
            {
                "id": "right",
                "name": "Right",
                "operator": "SELECT",
                "dependencies": [],
                "parameters": {
                    "kind": "SELECT",
                    "entity_id": "commerce.sales_record",
                    "columns": ["commerce.sales_record.region"],
                },
            },
            {
                "id": "join",
                "name": "Join",
                "operator": "JOIN",
                "dependencies": ["left", "right"],
                "parameters": {
                    "kind": "JOIN",
                    "relation_id": "relation.sales_to_plan",
                    "left_key": "commerce.sales_record.region",
                    "right_key": "commerce.sales_record.region",
                    "join_type": "inner",
                },
            },
        ],
        "output_node_id": "join",
        "result_schema": [],
    }
    context = SemanticContext(
        ontology_id=registry.document.ontology_id,
        ontology_version=registry.document.version,
        entities=registry.document.entities,
        fields=registry.document.fields,
        metrics=registry.document.metrics,
        relations=registry.document.relations,
    )

    result_codes = codes(registry, context, candidate)
    assert "ONTOLOGY_CONCEPT_FORBIDDEN" in result_codes
    assert "JOIN_OUTPUT_COLLISION" in result_codes
