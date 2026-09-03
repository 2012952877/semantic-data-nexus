"""Compare candidate semantic artifacts with versioned golden cases."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

DEFAULT_WEIGHTS = {
    "semantic": 30.0,
    "plan": 25.0,
    "execution_result": 25.0,
    "governance": 10.0,
    "observability": 10.0,
}


@dataclass
class Difference:
    dimension: str
    path: str
    expected: Any
    actual: Any
    message: str


@dataclass
class CaseReport:
    case_id: str
    score: float
    dimension_scores: dict[str, float]
    differences: list[Difference] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.differences


@dataclass
class EvaluationReport:
    suite_version: str
    score: float
    dimension_scores: dict[str, float]
    cases: list[CaseReport]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        result = _normalize_temporal(asdict(self))
        result["passed"] = self.passed
        for case, encoded in zip(self.cases, result["cases"], strict=True):
            encoded["passed"] = case.passed
        return result


def load_document(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    with source.open(encoding="utf-8") as handle:
        if source.suffix.lower() in {".yaml", ".yml"}:
            document = yaml.safe_load(handle)
        else:
            document = json.load(handle)
    document = _normalize_temporal(document)
    if not isinstance(document, dict):
        raise ValueError(f"{source} must contain an object at its root")
    return document


def _normalize_temporal(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            _normalize_temporal(key): _normalize_temporal(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_temporal(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_temporal(item) for item in value)
    return value


def _difference(
    differences: list[Difference],
    dimension: str,
    path: str,
    expected: Any,
    actual: Any,
    message: str | None = None,
) -> None:
    differences.append(
        Difference(
            dimension=dimension,
            path=path,
            expected=expected,
            actual=actual,
            message=message or f"{path} differs",
        )
    )


def validate_plan(plan: dict[str, Any]) -> list[str]:
    """Return generic graph integrity errors without assuming a compiler contract."""
    errors: list[str] = []
    operators = plan.get("operators", [])
    edges = plan.get("edges", [])
    nodes = plan.get("nodes", [])
    if not isinstance(operators, list):
        return ["operators must be a list"]
    if not isinstance(edges, list):
        return ["edges must be a list"]
    if not isinstance(nodes, list):
        return ["nodes must be a list"]
    if operators and not nodes:
        return ["plan has operators but no executable nodes"]
    if nodes and not operators:
        errors.append("plan has nodes but no operators")
    if edges and not nodes:
        errors.append("plan has edges but no executable nodes")
    if any(not isinstance(operator, str) or not operator for operator in operators):
        errors.append("every operator must be a non-empty string")

    valid_nodes = [node for node in nodes if isinstance(node, dict)]
    if len(valid_nodes) != len(nodes):
        errors.append("every node must be an object")
    node_ids = [node.get("id") for node in valid_nodes]
    invalid_ids = [node_id for node_id in node_ids if not isinstance(node_id, str) or not node_id]
    if invalid_ids:
        errors.append("every node must have a non-empty string ID")
    string_ids = [node_id for node_id in node_ids if isinstance(node_id, str) and node_id]
    duplicates = sorted(
        {node_id for node_id in string_ids if string_ids.count(node_id) > 1}
    )
    if duplicates:
        errors.append(f"duplicate node IDs: {', '.join(duplicates)}")
    known = set(string_ids)

    node_operators = [node.get("operator") for node in valid_nodes]
    if node_operators != operators:
        errors.append("node operator sequence does not match plan operators")

    declared_edges: set[tuple[str, str]] = set()
    valid_edge_count = 0
    for edge in edges:
        if (
            not isinstance(edge, list)
            or len(edge) != 2
            or not all(isinstance(endpoint, str) and endpoint for endpoint in edge)
        ):
            errors.append("every edge must contain two non-empty string node IDs")
            continue
        source, target = edge
        valid_edge_count += 1
        declared_edges.add((source, target))
        missing_endpoints = sorted({source, target} - known)
        if missing_endpoints:
            errors.append(
                f"edge {source}->{target} has missing nodes: {', '.join(missing_endpoints)}"
            )
    if len(declared_edges) != valid_edge_count:
        errors.append("plan contains duplicate edges")

    dependency_edges: set[tuple[str, str]] = set()
    adjacency = {node_id: set() for node_id in known}
    undirected = {node_id: set() for node_id in known}
    indegree = {node_id: 0 for node_id in known}
    node_data: dict[str, dict[str, set[str]]] = {}
    for node in valid_nodes:
        node_id = node.get("id", "<missing>")
        sanitized: dict[str, set[str]] = {}
        for field_name in ("depends_on", "inputs", "outputs"):
            raw_values = node.get(field_name, [])
            if not isinstance(raw_values, list):
                errors.append(f"{node_id} {field_name} must be a list")
                raw_values = []
            values = [
                value
                for value in raw_values
                if isinstance(value, str) and value
            ]
            if len(values) != len(raw_values):
                errors.append(f"{node_id} has invalid {field_name}")
            if len(set(values)) != len(values):
                errors.append(f"{node_id} has duplicate {field_name}")
            sanitized[field_name] = set(values)
        if not sanitized["outputs"]:
            errors.append(f"{node_id} must declare at least one output")
        if isinstance(node_id, str) and node_id:
            node_data[node_id] = sanitized

    for node_id, data in node_data.items():
        string_dependencies = data["depends_on"]
        missing = sorted(string_dependencies - known)
        if missing:
            errors.append(f"{node_id} has missing dependencies: {', '.join(missing)}")
        for dependency in string_dependencies & known:
            dependency_edges.add((dependency, node_id))
            undirected[dependency].add(node_id)
            undirected[node_id].add(dependency)
            if node_id not in adjacency[dependency]:
                adjacency[dependency].add(node_id)
                indegree[node_id] += 1
        upstream_outputs = {
            output
            for dependency in string_dependencies
            for output in node_data.get(dependency, {}).get("outputs", set())
        }
        missing_inputs = sorted(data["inputs"] - upstream_outputs)
        if string_dependencies and not data["inputs"]:
            errors.append(f"{node_id} must declare inputs from its dependencies")
        elif string_dependencies and missing_inputs:
            errors.append(f"{node_id} has unavailable inputs: {', '.join(missing_inputs)}")

    if declared_edges != dependency_edges:
        errors.append("plan edges do not match node dependencies")

    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        node_id = ready.pop()
        visited += 1
        for downstream in adjacency[node_id]:
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                ready.append(downstream)
    if visited != len(known):
        errors.append("plan graph contains a dependency cycle")
    if known:
        connected = set()
        pending = [next(iter(known))]
        while pending:
            node_id = pending.pop()
            if node_id in connected:
                continue
            connected.add(node_id)
            pending.extend(undirected[node_id] - connected)
        if connected != known:
            errors.append("plan graph contains disconnected nodes")
    return errors


def _compare_exact(
    dimension: str,
    path: str,
    expected: Any,
    actual: Any,
    differences: list[Difference],
    *,
    unordered: bool = False,
) -> bool:
    left = sorted(expected) if unordered and isinstance(expected, list) else expected
    right = sorted(actual) if unordered and isinstance(actual, list) else actual
    if left == right:
        return True
    _difference(differences, dimension, path, expected, actual)
    return False


def _numbers_equal(expected: Any, actual: Any, tolerance: float) -> bool:
    if expected is None or actual is None:
        return expected is actual
    if isinstance(expected, bool) or isinstance(actual, bool):
        return (
            isinstance(expected, bool)
            and isinstance(actual, bool)
            and expected == actual
        )
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(float(expected), float(actual), abs_tol=tolerance, rel_tol=0.0)
    return expected == actual


def compare_rows(
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
    tolerance: float,
) -> tuple[bool, str | None]:
    if len(expected) != len(actual):
        return False, f"row count differs: expected {len(expected)}, got {len(actual)}"
    for row_index, (expected_row, actual_row) in enumerate(
        zip(expected, actual, strict=True)
    ):
        if set(expected_row) != set(actual_row):
            return False, f"row {row_index} columns differ"
        for column, expected_value in expected_row.items():
            actual_value = actual_row[column]
            if not _numbers_equal(expected_value, actual_value, tolerance):
                return (
                    False,
                    f"row {row_index} column {column} differs: "
                    f"expected {expected_value!r}, got {actual_value!r}",
                )
    return True, None


def _dimension_score(checks: Iterable[bool]) -> float:
    values = list(checks)
    return 100.0 * sum(values) / len(values) if values else 100.0


def evaluate_case(golden: dict[str, Any], actual: dict[str, Any] | None) -> CaseReport:
    case_id = golden["id"]
    expected = golden["expected"]
    differences: list[Difference] = []
    if actual is None:
        for dimension in DEFAULT_WEIGHTS:
            _difference(
                differences,
                dimension,
                f"cases.{case_id}",
                "candidate case",
                None,
                "candidate case is missing",
            )
        return CaseReport(
            case_id=case_id,
            score=0.0,
            dimension_scores={dimension: 0.0 for dimension in DEFAULT_WEIGHTS},
            differences=differences,
        )

    semantic_checks = []
    for key in ("entities", "fields", "metrics", "relations"):
        semantic_checks.append(
            _compare_exact(
                "semantic",
                f"semantic.{key}",
                expected["semantic"].get(key, []),
                actual.get("semantic", {}).get(key, []),
                differences,
                unordered=True,
            )
        )
    for key in ("member_normalization", "time_range"):
        semantic_checks.append(
            _compare_exact(
                "semantic",
                key,
                expected.get(key, {}),
                actual.get(key, {}),
                differences,
            )
        )

    expected_plan = expected.get("plan", {})
    actual_plan = actual.get("plan", {})
    plan_checks = [
        _compare_exact(
            "plan",
            "plan.operators",
            expected_plan.get("operators", []),
            actual_plan.get("operators", []),
            differences,
        ),
        _compare_exact(
            "plan",
            "plan.edges",
            expected_plan.get("edges", []),
            actual_plan.get("edges", []),
            differences,
        ),
        _compare_exact(
            "plan",
            "plan.nodes",
            expected_plan.get("nodes", []),
            actual_plan.get("nodes", []),
            differences,
        ),
    ]
    plan_errors = validate_plan(actual_plan)
    plan_checks.append(not plan_errors)
    if plan_errors:
        _difference(
            differences,
            "plan",
            "plan.nodes",
            "valid graph",
            plan_errors,
            "; ".join(plan_errors),
        )

    expected_result = expected.get("result", {})
    actual_result = actual.get("result", {})
    result_checks = []
    for key in ("schema", "grain", "order_by"):
        result_checks.append(
            _compare_exact(
                "execution_result",
                f"result.{key}",
                expected_result.get(key, []),
                actual_result.get(key, []),
                differences,
            )
        )
    tolerance = float(expected_result.get("tolerance", 0.0))
    rows_match, rows_message = compare_rows(
        expected_result.get("rows", []),
        actual_result.get("rows", []),
        tolerance,
    )
    result_checks.append(rows_match)
    if not rows_match:
        _difference(
            differences,
            "execution_result",
            "result.rows",
            expected_result.get("rows", []),
            actual_result.get("rows", []),
            rows_message,
        )

    governance_checks = [
        _compare_exact(
            "governance",
            "behavior",
            expected.get("behavior", {}),
            actual.get("behavior", {}),
            differences,
        ),
        _compare_exact(
            "governance",
            "governance",
            expected.get("governance", {}),
            actual.get("governance", {}),
            differences,
        ),
    ]

    lineage_required = expected.get("observability", {}).get("lineage_required", False)
    actual_lineage = actual.get("lineage")
    lineage_present = not lineage_required or bool(actual_lineage)
    observability_checks = [lineage_present]
    if not lineage_present:
        _difference(
            differences,
            "observability",
            "lineage",
            "present",
            actual_lineage,
            "required lineage is missing",
        )
    if lineage_required and actual_lineage:
        observability_checks.append(
            _compare_exact(
                "observability",
                "lineage.entities",
                expected["semantic"].get("entities", []),
                actual_lineage.get("entities", []),
                differences,
                unordered=True,
            )
        )

    dimension_scores = {
        "semantic": _dimension_score(semantic_checks),
        "plan": _dimension_score(plan_checks),
        "execution_result": _dimension_score(result_checks),
        "governance": _dimension_score(governance_checks),
        "observability": _dimension_score(observability_checks),
    }
    score = sum(
        dimension_scores[dimension] * weight / 100.0
        for dimension, weight in DEFAULT_WEIGHTS.items()
    )
    return CaseReport(
        case_id=case_id,
        score=round(score, 2),
        dimension_scores={key: round(value, 2) for key, value in dimension_scores.items()},
        differences=differences,
    )


def evaluate_bundle(
    golden_suite: dict[str, Any],
    candidate_bundle: dict[str, Any],
) -> EvaluationReport:
    golden_suite = _normalize_temporal(golden_suite)
    candidate_bundle = _normalize_temporal(candidate_bundle)
    candidates = candidate_bundle.get("cases", {})
    if isinstance(candidates, list):
        candidates = {candidate["id"]: candidate for candidate in candidates}
    reports = [
        evaluate_case(case, candidates.get(case["id"]))
        for case in golden_suite.get("cases", [])
    ]
    dimension_scores = {
        dimension: round(
            sum(case.dimension_scores[dimension] for case in reports) / len(reports),
            2,
        )
        if reports
        else 0.0
        for dimension in DEFAULT_WEIGHTS
    }
    score = sum(
        dimension_scores[dimension] * weight / 100.0
        for dimension, weight in DEFAULT_WEIGHTS.items()
    )
    return EvaluationReport(
        suite_version=golden_suite.get("suite_version", "unknown"),
        score=round(score, 2),
        dimension_scores=dimension_scores,
        cases=reports,
    )
