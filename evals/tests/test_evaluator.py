from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest

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
    assert compare_rows([{"value": 0.1}], [{"value": 0.2}], 0.1)[0]
    assert compare_rows([{"value": 0.2}], [{"value": 0.1}], 0.1)[0]


def test_evaluator_preserves_large_integer_tolerance() -> None:
    golden_case = copy.deepcopy(suite()["cases"][0])
    case_id = golden_case["id"]
    candidate_case = copy.deepcopy(passing()["cases"][case_id])
    golden_case["expected"]["result"]["rows"] = [{"value": 0}]
    golden_case["expected"]["result"]["tolerance"] = 9007199254740995
    candidate_case["result"]["rows"] = [{"value": 9007199254740996}]
    report = evaluate_bundle(
        {"suite_version": "test", "cases": [golden_case]},
        {"cases": {case_id: candidate_case}},
    )
    case = report.cases[0]
    assert any(
        difference.path == "result.rows" for difference in case.differences
    )


def test_large_integer_pairs_compare_exactly() -> None:
    large = 2**53
    assert compare_rows([{"value": large}], [{"value": large}], 100)[0]
    matched, message = compare_rows(
        [{"value": large}],
        [{"value": large + 1}],
        100,
    )
    assert not matched
    assert "value differs" in str(message)
    mixed, mixed_message = compare_rows(
        [{"value": 1.0}],
        [{"value": 10**400}],
        0,
    )
    assert not mixed
    assert "value differs" in str(mixed_message)
    rounded_float = float(large + 1)
    zero_tolerance, zero_message = compare_rows(
        [{"value": large + 1}],
        [{"value": rounded_float}],
        0,
    )
    assert not zero_tolerance
    assert "value differs" in str(zero_message)
    assert compare_rows(
        [{"value": large + 1}],
        [{"value": rounded_float}],
        1,
    )[0]


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


def test_null_plan_and_malformed_nested_values_report_without_crashing() -> None:
    golden_suite = suite()
    case_id = "regional-quarterly-gross-profit"

    null_plan = copy.deepcopy(passing())
    null_plan["cases"][case_id]["plan"] = None
    null_report = evaluate_bundle(golden_suite, null_plan)
    null_case = next(case for case in null_report.cases if case.case_id == case_id)
    assert null_case.dimension_scores["plan"] < 100
    assert any(
        "INVALID_PLAN_SHAPE" in difference.message
        for difference in null_case.differences
    )

    malformed = copy.deepcopy(passing())
    actual = malformed["cases"][case_id]
    actual["semantic"]["entities"] = [{"not": "a string"}]
    actual["result"]["rows"] = [[]]
    actual["lineage"]["entities"] = None
    malformed_report = evaluate_bundle(golden_suite, malformed)
    malformed_case = next(
        case for case in malformed_report.cases if case.case_id == case_id
    )
    assert not malformed_case.passed
    assert {
        difference.dimension for difference in malformed_case.differences
    } >= {"semantic", "execution_result", "observability"}
    assert any(
        difference.dimension == "semantic"
        and "INVALID_CANDIDATE_SHAPE" in difference.message
        for difference in malformed_case.differences
    )
    json.dumps(malformed_report.to_dict())


def test_list_candidates_reject_duplicate_missing_and_empty_ids() -> None:
    golden_case = copy.deepcopy(suite()["cases"][0])
    case_id = golden_case["id"]
    valid = copy.deepcopy(passing()["cases"][case_id])
    valid["id"] = case_id
    malformed_duplicate = copy.deepcopy(valid)
    malformed_duplicate["plan"] = None

    duplicate_report = evaluate_bundle(
        {"suite_version": "test", "cases": [golden_case]},
        {"cases": [malformed_duplicate, valid]},
    )
    assert duplicate_report.score == 0
    assert not duplicate_report.passed
    assert any("duplicate ID" in error for error in duplicate_report.validation_errors)

    invalid_id_report = evaluate_bundle(
        {"suite_version": "test", "cases": [golden_case]},
        {"cases": [{"id": ""}, {"semantic": {}}]},
    )
    assert invalid_id_report.score == 0
    assert len(invalid_id_report.validation_errors) == 2
    assert all(
        "non-empty string ID" in error
        for error in invalid_id_report.validation_errors
    )


def test_yaml_native_values_are_rejected_with_serializable_diagnostics(
    tmp_path: Path,
) -> None:
    candidate_path = tmp_path / "native-values.yaml"
    candidate_path.write_text(
        "artifact_version: candidate-v0\n"
        "blob: !!binary SGVsbG8=\n"
        "cases:\n"
        "  regional-quarterly-gross-profit:\n"
        "    semantic:\n"
        "      entities: !!set\n"
        "        order: null\n",
        encoding="utf-8",
    )
    golden_case = copy.deepcopy(suite()["cases"][0])
    report = evaluate_bundle(
        {"suite_version": "test", "cases": [golden_case]},
        load_document(candidate_path),
    )
    assert report.score == 0
    assert not report.passed
    assert any("YAML binary" in error for error in report.validation_errors)
    assert any("YAML set" in error for error in report.validation_errors)
    json.dumps(report.to_dict())

    with pytest.raises(ValueError, match="non-JSON-compatible"):
        evaluate_bundle(
            {"suite_version": "test", "cases": [], "invalid": {1, 2}},
            {"cases": {}},
        )


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        (
            "duplicate.json",
            '{"cases": {}, "cases": []}',
            "duplicate JSON mapping key",
        ),
        (
            "duplicate.yaml",
            "cases: {}\ncases: []\n",
            "duplicate YAML mapping key",
        ),
    ],
)
def test_duplicate_parser_keys_are_rejected(
    tmp_path: Path,
    filename: str,
    content: str,
    message: str,
) -> None:
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_document(path)


def test_mapping_key_collision_after_canonicalization_is_rejected() -> None:
    golden_case = copy.deepcopy(suite()["cases"][0])
    candidate = copy.deepcopy(passing())
    candidate["metadata"] = {
        date(2024, 1, 1): "temporal",
        "2024-01-01": "string",
    }
    report = evaluate_bundle(
        {"suite_version": "test", "cases": [golden_case]},
        candidate,
    )
    assert any(
        "collision after canonicalization" in error
        for error in report.validation_errors
    )
    json.dumps(report.to_dict(), ensure_ascii=False)


def test_suite_declared_weights_control_case_and_bundle_scores() -> None:
    golden_case = copy.deepcopy(suite()["cases"][0])
    candidate_case = copy.deepcopy(passing()["cases"][golden_case["id"]])
    candidate_case["semantic"]["metrics"] = ["sales"]
    candidate = {"cases": {golden_case["id"]: candidate_case}}

    semantic_only = {
        "suite_version": "test",
        "weights": {
            "semantic": 100,
            "plan": 0,
            "execution_result": 0,
            "governance": 0,
            "observability": 0,
        },
        "cases": [golden_case],
    }
    semantic_report = evaluate_bundle(semantic_only, candidate)
    assert semantic_report.score == semantic_report.dimension_scores["semantic"]

    plan_only = copy.deepcopy(semantic_only)
    plan_only["weights"] = {
        "semantic": 0,
        "plan": 1,
        "execution_result": 0,
        "governance": 0,
        "observability": 0,
    }
    plan_report = evaluate_bundle(plan_only, candidate)
    assert plan_report.score == 100
    assert not plan_report.passed


@pytest.mark.parametrize(
    "weights",
    [
        None,
        {"semantic": 1},
        {
            "semantic": True,
            "plan": 1,
            "execution_result": 1,
            "governance": 1,
            "observability": 1,
        },
        {
            "semantic": 0,
            "plan": 0,
            "execution_result": 0,
            "governance": 0,
            "observability": 0,
        },
        {
            "semantic": 1e308,
            "plan": 1e308,
            "execution_result": 1e308,
            "governance": 1e308,
            "observability": 1e308,
        },
        {
            "semantic": 10**400,
            "plan": 1,
            "execution_result": 1,
            "governance": 1,
            "observability": 1,
        },
    ],
)
def test_invalid_suite_weights_are_rejected(weights) -> None:
    golden_suite = {
        "suite_version": "test",
        "weights": weights,
        "cases": [],
    }
    with pytest.raises(ValueError, match="suite weight"):
        evaluate_bundle(golden_suite, {"cases": {}})


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


def test_yaml_temporal_scalars_are_canonicalized_during_evaluation(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "candidate.yaml"
    yaml_path.write_text(
        "start: 2025-01-01\n"
        "clock: 2025-04-15T09:00:00+08:00\n"
        "dates:\n"
        "  - 2024-03-01\n"
        "2024-01-02: keyed-date\n",
        encoding="utf-8",
    )
    document = load_document(yaml_path)
    assert isinstance(document["start"], date)

    golden_case = copy.deepcopy(
        next(
            case
            for case in suite()["cases"]
            if case["id"] == "relative-last-quarter-sales"
        )
    )
    candidate_case = copy.deepcopy(passing()["cases"][golden_case["id"]])
    candidate_case["time_range"]["start"] = document["start"]
    candidate_case["time_range"]["evaluation_clock"] = document["clock"]
    candidate_case["metadata"] = {
        key: value
        for key, value in document.items()
        if isinstance(key, date)
    }
    report = evaluate_bundle(
        {"suite_version": "test", "cases": [golden_case]},
        {"cases": {golden_case["id"]: candidate_case}},
    )
    assert report.passed
    json.dumps(report.to_dict())


def test_yaml_date_candidate_id_is_rejected_before_normalization(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "date-id.yaml"
    yaml_path.write_text(
        "artifact_version: candidate-v0\n"
        "cases:\n"
        "  - id: 2024-01-01\n",
        encoding="utf-8",
    )
    golden_case = copy.deepcopy(suite()["cases"][0])
    golden_case["id"] = "2024-01-01"
    report = evaluate_bundle(
        {"suite_version": "test", "cases": [golden_case]},
        load_document(yaml_path),
    )
    assert report.score == 0
    assert any("non-empty string ID" in error for error in report.validation_errors)


def test_recursive_yaml_alias_is_rejected_without_recursion_error(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "recursive.yaml"
    yaml_path.write_text(
        "artifact_version: candidate-v0\n"
        "loop: &loop\n"
        "  - *loop\n"
        "cases: {}\n",
        encoding="utf-8",
    )
    report = evaluate_bundle(
        {"suite_version": "test", "cases": [copy.deepcopy(suite()["cases"][0])]},
        load_document(yaml_path),
    )
    assert any("recursive YAML alias" in error for error in report.validation_errors)
    json.dumps(report.to_dict())


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
