from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

from semantic_eval.evaluator import (
    compare_rows,
    evaluate_bundle,
    load_document,
    validate_plan,
)
from semantic_eval.reference import load_static_candidate


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "evals" / "fixtures" / "v0"


def suite():
    return load_document(FIXTURES / "golden_cases.json")


def passing():
    return load_static_candidate(FIXTURES / "candidates" / "passing.json")


def test_passing_reference_scores_100() -> None:
    report = evaluate_bundle(suite(), passing())
    assert report.passed
    assert report.score == 100.0
    assert set(report.dimension_scores.values()) == {100.0}


def test_failing_reference_has_precise_diagnostics() -> None:
    candidate = load_static_candidate(FIXTURES / "candidates" / "failing.json")
    report = evaluate_bundle(suite(), candidate)
    assert not report.passed
    assert report.score < 100
    paths = {
        (difference.dimension, difference.path)
        for case in report.cases
        for difference in case.differences
    }
    assert ("semantic", "semantic.metrics") in paths
    assert ("plan", "plan.operators") in paths
    assert ("execution_result", "result.rows") in paths
    assert ("governance", "governance") in paths
    assert ("observability", "lineage") in paths


def test_numeric_tolerance_is_absolute() -> None:
    expected = [{"gross_margin": 0.4}]
    assert compare_rows(expected, [{"gross_margin": 0.40009}], 0.0001)[0]
    matched, message = compare_rows(expected, [{"gross_margin": 0.401}], 0.0001)
    assert not matched
    assert "gross_margin differs" in str(message)


def test_boolean_is_not_accepted_as_a_number() -> None:
    matched, message = compare_rows([{"order_count": 1}], [{"order_count": True}], 0)
    assert not matched
    assert "order_count differs" in str(message)


def test_missing_candidate_case_scores_zero() -> None:
    golden = {"suite_version": "test", "cases": [suite()["cases"][0]]}
    report = evaluate_bundle(golden, {"cases": {}})
    assert report.score == 0
    assert len(report.cases[0].differences) == 5


def test_relative_clock_change_is_semantic_failure() -> None:
    golden_suite = suite()
    candidate = passing()
    changed = copy.deepcopy(candidate)
    changed["cases"]["relative-last-quarter-sales"]["time_range"][
        "evaluation_clock"
    ] = "2025-04-16T09:00:00+08:00"
    report = evaluate_bundle(golden_suite, changed)
    case = next(
        item for item in report.cases if item.case_id == "relative-last-quarter-sales"
    )
    assert any(item.path == "time_range" for item in case.differences)


def test_generic_plan_validation() -> None:
    fixtures = load_document(FIXTURES / "candidates" / "invalid_plans.json")[
        "fixtures"
    ]
    messages = {
        fixture["expected_diagnostic"]: "; ".join(validate_plan(fixture["plan"]))
        for fixture in fixtures
    }
    assert "duplicate node IDs" in messages["DUPLICATE_NODE_ID"]
    assert "missing dependencies" in messages["MISSING_DEPENDENCY"]
    assert "unavailable inputs" in messages["INVALID_COLUMN_FLOW"]
    assert "dependency cycle" in messages["DEPENDENCY_CYCLE"]
    assert "no executable nodes" in messages["EMPTY_NODE_GRAPH"]
    assert "non-empty string ID" in messages["MISSING_NODE_ID"]
    assert "disconnected nodes" in messages["DISCONNECTED_GRAPH"]
    assert "outputs must be a list" in messages["MALFORMED_NODE_FIELDS"]


def test_plan_nodes_are_required_and_structurally_consistent() -> None:
    golden_suite = suite()
    candidate = passing()
    case_id = "regional-quarterly-gross-profit"

    missing_nodes = copy.deepcopy(candidate)
    missing_nodes["cases"][case_id]["plan"]["nodes"] = []
    missing_report = evaluate_bundle(golden_suite, missing_nodes)
    missing_case = next(case for case in missing_report.cases if case.case_id == case_id)
    assert missing_case.dimension_scores["plan"] < 100
    assert "no executable nodes" in missing_case.differences[-1].message

    inconsistent = copy.deepcopy(candidate)
    inconsistent["cases"][case_id]["plan"]["nodes"][0]["operator"] = "PROJECT"
    inconsistent_report = evaluate_bundle(golden_suite, inconsistent)
    inconsistent_case = next(
        case for case in inconsistent_report.cases if case.case_id == case_id
    )
    assert inconsistent_case.dimension_scores["plan"] < 100
    assert "operator sequence" in inconsistent_case.differences[-1].message


def test_disconnected_and_malformed_node_fields_return_diagnostics() -> None:
    disconnected = {
        "operators": ["SCAN", "SCAN"],
        "edges": [],
        "nodes": [
            {
                "id": "left",
                "operator": "SCAN",
                "depends_on": [],
                "inputs": [],
                "outputs": ["left_id"],
            },
            {
                "id": "right",
                "operator": "SCAN",
                "depends_on": [],
                "inputs": [],
                "outputs": ["right_id"],
            },
        ],
    }
    assert "disconnected nodes" in "; ".join(validate_plan(disconnected))

    malformed = {
        "operators": ["SCAN", "PROJECT"],
        "edges": [["scan", "project"]],
        "nodes": [
            {
                "id": "scan",
                "operator": "SCAN",
                "depends_on": [],
                "inputs": [],
                "outputs": None,
            },
            {
                "id": "project",
                "operator": "PROJECT",
                "depends_on": ["scan"],
                "inputs": [[]],
                "outputs": ["rows"],
            },
        ],
    }
    diagnostics = "; ".join(validate_plan(malformed))
    assert "outputs must be a list" in diagnostics
    assert "invalid inputs" in diagnostics


def test_yaml_temporal_scalars_are_canonical_strings(tmp_path: Path) -> None:
    yaml_path = tmp_path / "candidate.yaml"
    yaml_path.write_text(
        "start: 2024-01-01\n"
        "clock: 2025-04-15T09:00:00+08:00\n"
        "dates:\n"
        "  - 2024-03-01\n"
        "2024-01-02: keyed-date\n",
        encoding="utf-8",
    )
    document = load_document(yaml_path)
    assert document == {
        "start": "2024-01-01",
        "clock": "2025-04-15T09:00:00+08:00",
        "dates": ["2024-03-01"],
        "2024-01-02": "keyed-date",
    }
    json.dumps(document)


def test_in_memory_temporal_values_compare_and_serialize() -> None:
    golden_case = copy.deepcopy(suite()["cases"][0])
    candidate_case = copy.deepcopy(passing()["cases"][golden_case["id"]])
    candidate_case["time_range"]["start"] = date(2024, 1, 1)
    report = evaluate_bundle(
        {"suite_version": "test", "cases": [golden_case]},
        {"cases": {golden_case["id"]: candidate_case}},
    )
    assert report.passed
    json.dumps(report.to_dict())
