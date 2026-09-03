from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from semantic_api.models import (
    CompilationMode,
    QueryPolicy,
    ResolutionSource,
    ResolvedTerm,
    ResolvedTermKind,
    SemanticContext,
    TimeWindow,
)
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


def test_project_alias_preserves_member_domain(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    candidate = {
        "schema_version": "sqg.v0",
        "nodes": [
            {
                "id": "select",
                "name": "Select region",
                "operator": "SELECT",
                "dependencies": [],
                "parameters": {
                    "kind": "SELECT",
                    "entity_id": "commerce.sales_record",
                    "columns": ["commerce.sales_record.region"],
                },
            },
            {
                "id": "project",
                "name": "Alias region",
                "operator": "PROJECT",
                "dependencies": ["select"],
                "parameters": {
                    "kind": "PROJECT",
                    "columns": [
                        {
                            "source": "commerce.sales_record.region",
                            "alias": "region_alias",
                        }
                    ],
                },
            },
            {
                "id": "filter",
                "name": "Filter raw label",
                "operator": "FILTER",
                "dependencies": ["project"],
                "parameters": {
                    "kind": "FILTER",
                    "predicate": {
                        "column": "region_alias",
                        "operator": "eq",
                        "value": "East",
                    },
                },
            },
        ],
        "output_node_id": "filter",
        "result_schema": [{"name": "region_alias", "data_type": "string"}],
    }

    assert "ONTOLOGY_MEMBER_UNKNOWN" in codes(registry, semantic_context, candidate)


def test_exact_member_and_time_constraint_coverage(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    member = ResolvedTerm(
        source_text="East",
        kind=ResolvedTermKind.MEMBER,
        machine_id="region.east",
        resolution_source=ResolutionSource.LABEL,
    )
    window = TimeWindow(
        source_text="去年",
        start="2025-01-01T00:00:00+08:00",
        end_exclusive="2026-01-01T00:00:00+08:00",
        timezone="Asia/Shanghai",
        grain="year",
    )
    candidate = StaticFixtureProvider._quarterly_profit([window], [member], semantic_context)
    validator = SQGValidator(registry)

    assert validator.validate(candidate, semantic_context, [member], [window]).valid
    missing = validator.validate(
        StaticFixtureProvider._quarterly_profit([]),
        semantic_context,
        [member],
        [window],
    )
    assert {"MISSING_MEMBER_CONSTRAINT", "MISSING_TIME_CONSTRAINT"} <= {
        item.code for item in missing.diagnostics
    }
    extra = validator.validate(candidate, semantic_context)
    assert {"UNRESOLVED_MEMBER_CONSTRAINT", "UNRESOLVED_TIME_CONSTRAINT"} <= {
        item.code for item in extra.diagnostics
    }


def test_negative_member_predicate_does_not_satisfy_positive_coverage(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    member = ResolvedTerm(
        source_text="East",
        kind=ResolvedTermKind.MEMBER,
        machine_id="region.east",
        resolution_source=ResolutionSource.LABEL,
    )
    candidate = StaticFixtureProvider._quarterly_profit([], [member], semantic_context)
    candidate["nodes"][1]["parameters"]["predicate"]["operator"] = "ne"
    result = SQGValidator(registry).validate(candidate, semantic_context, [member], [])

    assert {"MEMBER_OPERATOR_UNSUPPORTED", "MISSING_MEMBER_CONSTRAINT"} <= {
        item.code for item in result.diagnostics
    }


def test_derived_datetime_does_not_satisfy_source_time_coverage(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    window = TimeWindow(
        source_text="去年",
        start="2025-01-01T00:00:00+08:00",
        end_exclusive="2026-01-01T00:00:00+08:00",
        timezone="Asia/Shanghai",
        grain="year",
    )
    candidate = {
        "schema_version": "sqg.v0",
        "nodes": [
            {
                "id": "select",
                "name": "Select period",
                "operator": "SELECT",
                "dependencies": [],
                "parameters": {
                    "kind": "SELECT",
                    "entity_id": "commerce.sales_record",
                    "columns": ["commerce.sales_record.period"],
                },
            },
            {
                "id": "derive",
                "name": "Derive unrelated time",
                "operator": "DERIVE",
                "dependencies": ["select"],
                "parameters": {
                    "kind": "DERIVE",
                    "columns": [
                        {
                            "output": "synthetic_time",
                            "data_type": "datetime",
                            "expression": {
                                "kind": "literal",
                                "value": "2025-06-01T00:00:00+08:00",
                                "data_type": "datetime",
                            },
                        }
                    ],
                },
            },
            {
                "id": "filter",
                "name": "Filter synthetic time",
                "operator": "FILTER",
                "dependencies": ["derive"],
                "parameters": {
                    "kind": "FILTER",
                    "predicate": {
                        "column": "synthetic_time",
                        "operator": "between",
                        "value": {
                            "start": window.start.isoformat(),
                            "end_exclusive": window.end_exclusive.isoformat(),
                        },
                    },
                },
            },
        ],
        "output_node_id": "filter",
        "result_schema": [],
    }
    result = SQGValidator(registry).validate(candidate, semantic_context, [], [window])

    assert "MISSING_TIME_CONSTRAINT" in {item.code for item in result.diagnostics}


def test_intersecting_member_filters_cannot_satisfy_requested_set(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    members = [
        ResolvedTerm(
            source_text=label,
            kind=ResolvedTermKind.MEMBER,
            machine_id=machine_id,
            resolution_source=ResolutionSource.LABEL,
        )
        for label, machine_id in (
            ("East", "region.east"),
            ("West", "region.west"),
        )
    ]
    candidate = StaticFixtureProvider._quarterly_profit([], members, semantic_context)
    candidate["nodes"][1]["parameters"]["predicate"].update(
        {"operator": "eq", "value": "region.east"}
    )
    candidate["nodes"].insert(
        2,
        {
            "id": "filter_member_2",
            "name": "Contradictory member filter",
            "operator": "FILTER",
            "dependencies": ["filter_member_1"],
            "parameters": {
                "kind": "FILTER",
                "predicate": {
                    "column": "commerce.sales_record.region",
                    "operator": "eq",
                    "value": "region.west",
                },
            },
        },
    )
    candidate["nodes"][3]["dependencies"] = ["filter_member_2"]
    result = SQGValidator(registry).validate(candidate, semantic_context, members, [])

    assert "MEMBER_CONSTRAINT_SHAPE_INVALID" in {item.code for item in result.diagnostics}


def test_additional_time_predicate_cannot_narrow_covered_window(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    window = TimeWindow(
        source_text="去年",
        start="2025-01-01T00:00:00+08:00",
        end_exclusive="2026-01-01T00:00:00+08:00",
        timezone="Asia/Shanghai",
        grain="year",
    )
    candidate = StaticFixtureProvider._quarterly_profit([window], [], semantic_context)
    candidate["nodes"].insert(
        2,
        {
            "id": "filter_extra_time",
            "name": "Unexpected time narrowing",
            "operator": "FILTER",
            "dependencies": ["filter_period_1"],
            "parameters": {
                "kind": "FILTER",
                "predicate": {
                    "column": "commerce.sales_record.period",
                    "operator": "gte",
                    "value": "2025-07-01T00:00:00+08:00",
                },
            },
        },
    )
    candidate["nodes"][3]["dependencies"] = ["filter_extra_time"]
    result = SQGValidator(registry).validate(candidate, semantic_context, [], [window])

    assert "TIME_CONSTRAINT_SHAPE_INVALID" in {item.code for item in result.diagnostics}


def test_pivot_output_preserves_member_domain_for_narrowing_detection(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    members = [
        ResolvedTerm(
            source_text=label,
            kind=ResolvedTermKind.MEMBER,
            machine_id=machine_id,
            resolution_source=ResolutionSource.LABEL,
        )
        for label, machine_id in (
            ("East", "region.east"),
            ("West", "region.west"),
        )
    ]
    candidate = {
        "schema_version": "sqg.v0",
        "nodes": [
            {
                "id": "select",
                "name": "Select",
                "operator": "SELECT",
                "dependencies": [],
                "parameters": {
                    "kind": "SELECT",
                    "entity_id": "commerce.sales_record",
                    "columns": [
                        "commerce.sales_record.region",
                        "commerce.sales_record.period",
                    ],
                },
            },
            {
                "id": "canonical",
                "name": "Canonical member set",
                "operator": "FILTER",
                "dependencies": ["select"],
                "parameters": {
                    "kind": "FILTER",
                    "predicate": {
                        "column": "commerce.sales_record.region",
                        "operator": "in",
                        "value": ["region.east", "region.west"],
                    },
                },
            },
            {
                "id": "pivot",
                "name": "Pivot member values",
                "operator": "PIVOT",
                "dependencies": ["canonical"],
                "parameters": {
                    "kind": "PIVOT",
                    "index": ["commerce.sales_record.period"],
                    "column": "commerce.sales_record.period",
                    "value": "commerce.sales_record.region",
                    "values": ["member_bucket"],
                },
            },
            {
                "id": "narrow",
                "name": "Narrow pivot member",
                "operator": "FILTER",
                "dependencies": ["pivot"],
                "parameters": {
                    "kind": "FILTER",
                    "predicate": {
                        "column": "member_bucket",
                        "operator": "eq",
                        "value": "region.east",
                    },
                },
            },
        ],
        "output_node_id": "narrow",
        "result_schema": [],
    }
    result = SQGValidator(registry).validate(candidate, semantic_context, members, [])

    assert "MEMBER_CONSTRAINT_SHAPE_INVALID" in {item.code for item in result.diagnostics}


def test_pivot_output_filter_cannot_originate_member_coverage(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    member = ResolvedTerm(
        source_text="East",
        kind=ResolvedTermKind.MEMBER,
        machine_id="region.east",
        resolution_source=ResolutionSource.LABEL,
    )
    candidate = {
        "schema_version": "sqg.v0",
        "nodes": [
            {
                "id": "select",
                "name": "Select",
                "operator": "SELECT",
                "dependencies": [],
                "parameters": {
                    "kind": "SELECT",
                    "entity_id": "commerce.sales_record",
                    "columns": [
                        "commerce.sales_record.region",
                        "commerce.sales_record.period",
                    ],
                },
            },
            {
                "id": "pivot",
                "name": "Pivot member",
                "operator": "PIVOT",
                "dependencies": ["select"],
                "parameters": {
                    "kind": "PIVOT",
                    "index": ["commerce.sales_record.period"],
                    "column": "commerce.sales_record.period",
                    "value": "commerce.sales_record.region",
                    "values": ["member_bucket"],
                },
            },
            {
                "id": "filter",
                "name": "Late member filter",
                "operator": "FILTER",
                "dependencies": ["pivot"],
                "parameters": {
                    "kind": "FILTER",
                    "predicate": {
                        "column": "member_bucket",
                        "operator": "eq",
                        "value": "region.east",
                    },
                },
            },
        ],
        "output_node_id": "filter",
        "result_schema": [],
    }
    result = SQGValidator(registry).validate(candidate, semantic_context, [member], [])

    assert "MISSING_MEMBER_CONSTRAINT" in {item.code for item in result.diagnostics}


def test_pivot_output_preserves_time_domain_for_narrowing_detection(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    window = TimeWindow(
        source_text="去年",
        start="2025-01-01T00:00:00+08:00",
        end_exclusive="2026-01-01T00:00:00+08:00",
        timezone="Asia/Shanghai",
        grain="year",
    )
    candidate = {
        "schema_version": "sqg.v0",
        "nodes": [
            {
                "id": "select",
                "name": "Select",
                "operator": "SELECT",
                "dependencies": [],
                "parameters": {
                    "kind": "SELECT",
                    "entity_id": "commerce.sales_record",
                    "columns": ["commerce.sales_record.period"],
                },
            },
            {
                "id": "canonical",
                "name": "Canonical time",
                "operator": "FILTER",
                "dependencies": ["select"],
                "parameters": {
                    "kind": "FILTER",
                    "predicate": {
                        "column": "commerce.sales_record.period",
                        "operator": "between",
                        "value": {
                            "start": window.start.isoformat(),
                            "end_exclusive": window.end_exclusive.isoformat(),
                        },
                    },
                },
            },
            {
                "id": "pivot",
                "name": "Pivot time values",
                "operator": "PIVOT",
                "dependencies": ["canonical"],
                "parameters": {
                    "kind": "PIVOT",
                    "index": [],
                    "column": "commerce.sales_record.period",
                    "value": "commerce.sales_record.period",
                    "values": ["time_bucket"],
                },
            },
            {
                "id": "narrow",
                "name": "Narrow pivot time",
                "operator": "FILTER",
                "dependencies": ["pivot"],
                "parameters": {
                    "kind": "FILTER",
                    "predicate": {
                        "column": "time_bucket",
                        "operator": "gte",
                        "value": "2025-07-01T00:00:00+08:00",
                    },
                },
            },
        ],
        "output_node_id": "narrow",
        "result_schema": [],
    }
    result = SQGValidator(registry).validate(candidate, semantic_context, [], [window])

    assert "TIME_CONSTRAINT_SHAPE_INVALID" in {item.code for item in result.diagnostics}


def test_sort_column_flow(registry: OntologyRegistry, semantic_context: SemanticContext) -> None:
    candidate = StaticFixtureProvider._quarterly_profit([])
    candidate["nodes"][2]["parameters"]["keys"][0]["column"] = "invented"

    assert "COLUMN_NOT_AVAILABLE" in codes(registry, semantic_context, candidate)


@pytest.mark.parametrize(
    ("predicate", "expected"),
    [
        (
            {
                "column": "commerce.sales_record.region",
                "operator": "in",
                "value": "region.east",
            },
            "PREDICATE_CARDINALITY_INVALID",
        ),
        (
            {
                "column": "commerce.sales_record.period",
                "operator": "between",
                "value": {"start": "not-a-date", "end_exclusive": "2026-01-01"},
            },
            "DATETIME_LITERAL_INVALID",
        ),
        (
            {
                "column": "commerce.sales_record.period",
                "operator": "between",
                "value": {
                    "start": "2026-02-01T00:00:00+00:00",
                    "end_exclusive": "2026-01-01T00:00:00+00:00",
                },
            },
            "TIME_RANGE_INVALID",
        ),
    ],
)
def test_predicate_runtime_type_cardinality_and_ranges(
    registry: OntologyRegistry,
    semantic_context: SemanticContext,
    predicate: dict[str, object],
    expected: str,
) -> None:
    candidate = StaticFixtureProvider._quarterly_profit([])
    candidate["nodes"][1] = {
        "id": "aggregate_profit",
        "name": "Invalid predicate",
        "operator": "FILTER",
        "dependencies": ["select_sales"],
        "parameters": {"kind": "FILTER", "predicate": predicate},
    }

    assert expected in codes(registry, semantic_context, candidate)


def test_typed_literal_runtime_value_is_validated(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    candidate = StaticFixtureProvider._monthly_comparison()
    candidate["nodes"][3]["parameters"]["columns"][0]["expression"]["right"] = {
        "kind": "literal",
        "value": "not-a-number",
        "data_type": "number",
    }

    assert "LITERAL_VALUE_TYPE_INVALID" in codes(registry, semantic_context, candidate)


def test_non_finite_number_is_rejected_by_schema(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    candidate = StaticFixtureProvider._monthly_comparison()
    candidate["nodes"][3]["parameters"]["columns"][0]["expression"]["right"] = {
        "kind": "literal",
        "value": float("nan"),
        "data_type": "number",
    }

    assert "SCHEMA_INVALID" in codes(registry, semantic_context, candidate)


def test_extremely_large_integer_literal_does_not_overflow_validation(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    candidate = StaticFixtureProvider._monthly_comparison()
    candidate["nodes"][3]["parameters"]["columns"][0]["expression"]["right"] = {
        "kind": "literal",
        "value": 10**10000,
        "data_type": "number",
    }

    assert "LITERAL_VALUE_TYPE_INVALID" not in codes(registry, semantic_context, candidate)


def test_trusted_mode_output_requires_semantic_lineage(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    candidate = StaticFixtureProvider._monthly_comparison()
    candidate["nodes"][3]["parameters"]["columns"][0]["expression"] = {
        "kind": "literal",
        "value": 0,
        "data_type": "number",
    }
    result = SQGValidator(registry).validate(
        candidate,
        semantic_context,
        compilation_mode=CompilationMode.MONTHLY_REGIONAL_COMPARISON,
    )

    assert "COMPILATION_MODE_OUTPUT_LINEAGE_INVALID" in {item.code for item in result.diagnostics}


def test_trusted_monthly_mode_requires_exact_change_formula(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    candidate = StaticFixtureProvider._monthly_comparison()
    candidate["nodes"][3]["parameters"]["columns"][0]["expression"] = {
        "kind": "column",
        "column": "profit_current",
    }
    result = SQGValidator(registry).validate(
        candidate,
        semantic_context,
        compilation_mode=CompilationMode.MONTHLY_REGIONAL_COMPARISON,
    )

    assert "COMPILATION_MODE_DERIVATION_INVALID" in {item.code for item in result.diagnostics}


@pytest.mark.parametrize(
    "mode",
    [
        CompilationMode.REGIONAL_QUARTERLY_PROFIT,
        CompilationMode.MONTHLY_REGIONAL_COMPARISON,
    ],
)
def test_trusted_modes_require_exact_profit_aggregate(
    registry: OntologyRegistry,
    semantic_context: SemanticContext,
    mode: CompilationMode,
) -> None:
    candidate = (
        StaticFixtureProvider._quarterly_profit([])
        if mode is CompilationMode.REGIONAL_QUARTERLY_PROFIT
        else StaticFixtureProvider._monthly_comparison()
    )
    aggregate = next(node for node in candidate["nodes"] if node["operator"] == "AGGREGATE")
    aggregate["parameters"]["group_by"].append("metric.profit")
    aggregate["parameters"]["measures"] = []
    result = SQGValidator(registry).validate(
        candidate,
        semantic_context,
        compilation_mode=mode,
    )

    assert "COMPILATION_MODE_AGGREGATE_INVALID" in {item.code for item in result.diagnostics}


def test_transformed_member_filter_is_still_an_introduced_constraint(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    candidate = {
        "schema_version": "sqg.v0",
        "nodes": [
            {
                "id": "select",
                "name": "Select",
                "operator": "SELECT",
                "dependencies": [],
                "parameters": {
                    "kind": "SELECT",
                    "entity_id": "commerce.sales_record",
                    "columns": [
                        "commerce.sales_record.region",
                        "commerce.sales_record.period",
                    ],
                },
            },
            {
                "id": "pivot",
                "name": "Transform region",
                "operator": "PIVOT",
                "dependencies": ["select"],
                "parameters": {
                    "kind": "PIVOT",
                    "index": ["commerce.sales_record.period"],
                    "column": "commerce.sales_record.period",
                    "value": "commerce.sales_record.region",
                    "values": ["region_alias"],
                },
            },
            {
                "id": "filter",
                "name": "Narrow transformed region",
                "operator": "FILTER",
                "dependencies": ["pivot"],
                "parameters": {
                    "kind": "FILTER",
                    "predicate": {
                        "column": "region_alias",
                        "operator": "eq",
                        "value": "region.east",
                    },
                },
            },
        ],
        "output_node_id": "filter",
        "result_schema": [
            {"name": "commerce.sales_record.period", "data_type": "datetime"},
            {"name": "region_alias", "data_type": "string"},
        ],
    }

    assert "UNRESOLVED_MEMBER_CONSTRAINT" in codes(registry, semantic_context, candidate)


def test_resolved_metric_cannot_be_omitted(
    registry: OntologyRegistry, semantic_context: SemanticContext
) -> None:
    metric = ResolvedTerm(
        source_text="profit",
        kind=ResolvedTermKind.METRIC,
        machine_id="metric.profit",
        resolution_source=ResolutionSource.LABEL,
    )
    candidate = {
        "schema_version": "sqg.v0",
        "nodes": [
            {
                "id": "select",
                "name": "Only region",
                "operator": "SELECT",
                "dependencies": [],
                "parameters": {
                    "kind": "SELECT",
                    "entity_id": "commerce.sales_record",
                    "columns": ["commerce.sales_record.region"],
                },
            }
        ],
        "output_node_id": "select",
        "result_schema": [],
    }
    result = SQGValidator(registry).validate(candidate, semantic_context, [metric], [])

    assert "MISSING_RESOLVED_CONCEPT" in {item.code for item in result.diagnostics}


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
    assert "RELATION_ENDPOINT_MISMATCH" in result_codes
    assert "JOIN_KEY_NOT_GOVERNED" in result_codes


def test_join_key_type_and_relation_key_compatibility(
    registry: OntologyRegistry,
) -> None:
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
                    "entity_id": "commerce.plan_record",
                    "columns": ["commerce.plan_record.period"],
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
                    "right_key": "commerce.plan_record.period",
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
    assert "JOIN_KEY_NOT_GOVERNED" in result_codes
    assert "JOIN_KEY_TYPE_MISMATCH" in result_codes


def test_left_join_right_branch_constraint_does_not_dominate_output(
    registry: OntologyRegistry,
) -> None:
    document = registry.document.model_copy(deep=True)
    document.entities[1].enabled = True
    document.entities[1].query_policy = QueryPolicy.ALLOW
    for field in document.fields:
        if field.entity_id == "commerce.plan_record":
            field.enabled = True
            field.query_policy = QueryPolicy.ALLOW
    document.relations[0].enabled = True
    document.relations[0].query_policy = QueryPolicy.ALLOW
    enabled_registry = OntologyRegistry(document)
    context = SemanticContext(
        ontology_id=document.ontology_id,
        ontology_version=document.version,
        entities=document.entities,
        fields=document.fields,
        metrics=document.metrics,
        relations=document.relations,
    )
    window = TimeWindow(
        source_text="去年",
        start="2025-01-01T00:00:00+08:00",
        end_exclusive="2026-01-01T00:00:00+08:00",
        timezone="Asia/Shanghai",
        grain="year",
    )
    candidate = {
        "schema_version": "sqg.v0",
        "nodes": [
            {
                "id": "sales",
                "name": "Unfiltered sales",
                "operator": "SELECT",
                "dependencies": [],
                "parameters": {
                    "kind": "SELECT",
                    "entity_id": "commerce.sales_record",
                    "columns": ["commerce.sales_record.region"],
                },
            },
            {
                "id": "plan",
                "name": "Plan",
                "operator": "SELECT",
                "dependencies": [],
                "parameters": {
                    "kind": "SELECT",
                    "entity_id": "commerce.plan_record",
                    "columns": [
                        "commerce.plan_record.region",
                        "commerce.plan_record.period",
                    ],
                },
            },
            {
                "id": "plan_time",
                "name": "Filter plan time",
                "operator": "FILTER",
                "dependencies": ["plan"],
                "parameters": {
                    "kind": "FILTER",
                    "predicate": {
                        "column": "commerce.plan_record.period",
                        "operator": "between",
                        "value": {
                            "start": window.start.isoformat(),
                            "end_exclusive": window.end_exclusive.isoformat(),
                        },
                    },
                },
            },
            {
                "id": "join",
                "name": "Left join",
                "operator": "JOIN",
                "dependencies": ["sales", "plan_time"],
                "parameters": {
                    "kind": "JOIN",
                    "relation_id": "relation.sales_to_plan",
                    "left_key": "commerce.sales_record.region",
                    "right_key": "commerce.plan_record.region",
                    "join_type": "left",
                },
            },
        ],
        "output_node_id": "join",
        "result_schema": [],
    }
    result = SQGValidator(enabled_registry).validate(candidate, context, [], [window])

    assert "MISSING_TIME_CONSTRAINT" in {item.code for item in result.diagnostics}

    unrequested = SQGValidator(enabled_registry).validate(candidate, context)
    assert "UNRESOLVED_TIME_CONSTRAINT" in {item.code for item in unrequested.diagnostics}

    candidate["nodes"][3]["parameters"]["join_type"] = "inner"
    result = SQGValidator(enabled_registry).validate(candidate, context, [], [window])
    result_codes = {item.code for item in result.diagnostics}
    assert "MISSING_TIME_CONSTRAINT" in result_codes
    assert "UNRESOLVED_TIME_DOMAIN" in result_codes


def test_pivot_value_cannot_masquerade_as_governed_join_key(
    registry: OntologyRegistry,
) -> None:
    candidate = {
        "schema_version": "sqg.v0",
        "nodes": [
            {
                "id": "sales",
                "name": "Sales",
                "operator": "SELECT",
                "dependencies": [],
                "parameters": {
                    "kind": "SELECT",
                    "entity_id": "commerce.sales_record",
                    "columns": [
                        "commerce.sales_record.region",
                        "commerce.sales_record.period",
                    ],
                },
            },
            {
                "id": "pivot",
                "name": "Transform region values",
                "operator": "PIVOT",
                "dependencies": ["sales"],
                "parameters": {
                    "kind": "PIVOT",
                    "index": ["commerce.sales_record.period"],
                    "column": "commerce.sales_record.period",
                    "value": "commerce.sales_record.region",
                    "values": ["fabricated_key"],
                },
            },
            {
                "id": "plan",
                "name": "Plan",
                "operator": "SELECT",
                "dependencies": [],
                "parameters": {
                    "kind": "SELECT",
                    "entity_id": "commerce.plan_record",
                    "columns": ["commerce.plan_record.region"],
                },
            },
            {
                "id": "join",
                "name": "Join",
                "operator": "JOIN",
                "dependencies": ["pivot", "plan"],
                "parameters": {
                    "kind": "JOIN",
                    "relation_id": "relation.sales_to_plan",
                    "left_key": "fabricated_key",
                    "right_key": "commerce.plan_record.region",
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

    assert "JOIN_KEY_NOT_GOVERNED" in codes(registry, context, candidate)
