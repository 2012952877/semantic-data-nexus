from __future__ import annotations

import copy
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
        fixture["expected_diagnostic"]: validate_plan(fixture["plan"])[0]
        for fixture in fixtures
    }
    assert "duplicate node IDs" in messages["DUPLICATE_NODE_ID"]
    assert "missing dependencies" in messages["MISSING_DEPENDENCY"]
    assert "unavailable inputs" in messages["INVALID_COLUMN_FLOW"]
