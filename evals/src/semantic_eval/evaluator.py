"""Compare candidate semantic artifacts with versioned golden cases."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
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


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise ValueError("YAML mapping keys must be hashable") from error
        if duplicate:
            raise ValueError(f"duplicate YAML mapping key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON mapping key: {key!r}")
        result[key] = value
    return result


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
    validation_errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.validation_errors and all(case.passed for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        result, _ = _sanitize_json_value(asdict(self), "report")
        result["passed"] = self.passed
        for case, encoded in zip(self.cases, result["cases"], strict=True):
            encoded["passed"] = case.passed
        return result


def load_document(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    with source.open(encoding="utf-8") as handle:
        if source.suffix.lower() in {".yaml", ".yml"}:
            document = yaml.load(handle, Loader=_UniqueKeyLoader)
        else:
            document = json.load(handle, object_pairs_hook=_unique_json_object)
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


def _sanitize_json_value(
    value: Any,
    path: str,
    active: set[int] | None = None,
) -> tuple[Any, list[str]]:
    if active is None:
        active = set()
    if isinstance(value, datetime):
        return value.isoformat(), []
    if isinstance(value, date):
        return value.isoformat(), []
    if isinstance(value, str):
        sanitized = "".join(
            character if not "\ud800" <= character <= "\udfff" else "\ufffd"
            for character in value
        )
        errors = (
            [f"{path} contains an unpaired Unicode surrogate"]
            if sanitized != value
            else []
        )
        return sanitized, errors
    if value is None or isinstance(value, (int, bool)):
        return value, []
    if isinstance(value, float):
        if math.isfinite(value):
            return value, []
        return (
            {"__invalid_json_type__": "non-finite-float"},
            [f"{path} contains a non-finite float"],
        )
    if isinstance(value, bytes):
        return (
            {"__invalid_json_type__": "bytes", "length": len(value)},
            [f"{path} contains YAML binary data"],
        )
    if isinstance(value, (set, frozenset)):
        return (
            {"__invalid_json_type__": type(value).__name__, "item_count": len(value)},
            [f"{path} contains a YAML set"],
        )
    if isinstance(value, tuple):
        sanitized_items = []
        errors = [f"{path} contains a non-JSON tuple"]
        for index, item in enumerate(value):
            sanitized, item_errors = _sanitize_json_value(
                item,
                f"{path}[{index}]",
                active,
            )
            sanitized_items.append(sanitized)
            errors.extend(item_errors)
        return (
            {"__invalid_json_type__": "tuple", "items": sanitized_items},
            errors,
        )
    if isinstance(value, (dict, list)):
        identity = id(value)
        if identity in active:
            return (
                {"__invalid_json_type__": "recursive-reference"},
                [f"{path} contains a recursive YAML alias"],
            )
        active.add(identity)
        try:
            if isinstance(value, list):
                sanitized_items = []
                errors = []
                for index, item in enumerate(value):
                    sanitized, item_errors = _sanitize_json_value(
                        item,
                        f"{path}[{index}]",
                        active,
                    )
                    sanitized_items.append(sanitized)
                    errors.extend(item_errors)
                return sanitized_items, errors

            sanitized_map: dict[str, Any] = {}
            errors = []
            for index, (key, item) in enumerate(value.items()):
                if isinstance(key, str):
                    sanitized_key, key_errors = _sanitize_json_value(
                        key,
                        f"{path}.<key>",
                        active,
                    )
                    errors.extend(key_errors)
                elif isinstance(key, (date, datetime)):
                    sanitized_key = key.isoformat()
                else:
                    sanitized_key = f"__invalid_key_{index}_{type(key).__name__}__"
                    errors.append(
                        f"{path} contains non-string mapping key {type(key).__name__}"
                    )
                if sanitized_key in sanitized_map:
                    errors.append(
                        f"{path} has mapping key collision after canonicalization: "
                        f"{sanitized_key!r}"
                    )
                    sanitized_key = f"__colliding_key_{index}__"
                sanitized, item_errors = _sanitize_json_value(
                    item,
                    f"{path}.{sanitized_key}",
                    active,
                )
                sanitized_map[sanitized_key] = sanitized
                errors.extend(item_errors)
            return sanitized_map, errors
        finally:
            active.remove(identity)
    return (
        {"__invalid_json_type__": type(value).__name__},
        [f"{path} contains unsupported type {type(value).__name__}"],
    )


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


def validate_plan(plan: Any) -> list[str]:
    """Return generic graph integrity errors without assuming a compiler contract."""
    if not isinstance(plan, dict):
        return ["plan must be an object"]
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
    def canonical(value: Any) -> Any:
        value = _normalize_temporal(value)
        if isinstance(value, dict):
            return [
                [
                    f"{type(key).__name__}:{key!r}",
                    canonical(item),
                ]
                for key, item in sorted(
                    value.items(),
                    key=lambda pair: (type(pair[0]).__name__, repr(pair[0])),
                )
            ]
        if isinstance(value, (list, tuple)):
            return [canonical(item) for item in value]
        return value

    def sort_key(value: Any) -> str:
        return json.dumps(
            canonical(value),
            ensure_ascii=False,
            default=str,
        )

    left = (
        sorted(expected, key=sort_key)
        if unordered and isinstance(expected, list)
        else expected
    )
    right = (
        sorted(actual, key=sort_key)
        if unordered and isinstance(actual, list)
        else actual
    )
    if left == right:
        return True
    _difference(differences, dimension, path, expected, actual)
    return False


def _numbers_equal(expected: Any, actual: Any, tolerance: Any) -> bool:
    if expected is None or actual is None:
        return expected is actual
    if isinstance(expected, bool) or isinstance(actual, bool):
        return (
            isinstance(expected, bool)
            and isinstance(actual, bool)
            and expected == actual
        )
    if isinstance(expected, int) and isinstance(actual, int):
        return expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if (
            isinstance(expected, float)
            and not math.isfinite(expected)
            or isinstance(actual, float)
            and not math.isfinite(actual)
        ):
            return expected == actual
        try:
            expected_number = (
                Decimal(expected) if isinstance(expected, int) else Decimal(str(expected))
            )
            actual_number = (
                Decimal(actual) if isinstance(actual, int) else Decimal(str(actual))
            )
            allowed = Decimal(str(tolerance))
            difference = abs(expected_number - actual_number)
        except (InvalidOperation, OverflowError, ValueError):
            return False
        return difference <= allowed
    return expected == actual


def compare_rows(
    expected: Any,
    actual: Any,
    tolerance: Any,
) -> tuple[bool, str | None]:
    if not isinstance(expected, list) or not isinstance(actual, list):
        return False, "rows must be lists"
    if len(expected) != len(actual):
        return False, f"row count differs: expected {len(expected)}, got {len(actual)}"
    for row_index, (expected_row, actual_row) in enumerate(
        zip(expected, actual, strict=True)
    ):
        if not isinstance(expected_row, dict) or not isinstance(actual_row, dict):
            return False, f"row {row_index} must be an object"
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


def _container_type_errors(expected: Any, actual: Any, path: str) -> list[str]:
    errors: list[str] = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path} must be an object"]
        for key, expected_value in expected.items():
            if key in actual:
                errors.extend(
                    _container_type_errors(
                        expected_value,
                        actual[key],
                        f"{path}.{key}",
                    )
                )
    elif isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path} must be a list"]
        if expected and isinstance(expected[0], (dict, list)):
            for index, actual_item in enumerate(actual):
                errors.extend(
                    _container_type_errors(
                        expected[0],
                        actual_item,
                        f"{path}[{index}]",
                    )
                )
        elif expected:
            for index, actual_item in enumerate(actual):
                if isinstance(actual_item, (dict, list)):
                    errors.append(f"{path}[{index}] must be a scalar")
        elif path in {"plan.nodes", "result.schema", "result.rows"}:
            for index, actual_item in enumerate(actual):
                if not isinstance(actual_item, dict):
                    errors.append(f"{path}[{index}] must be an object")
        elif path == "plan.edges":
            for index, actual_item in enumerate(actual):
                if not isinstance(actual_item, list):
                    errors.append(f"{path}[{index}] must be a list")
        elif path in {
            "semantic.entities",
            "semantic.fields",
            "semantic.metrics",
            "semantic.relations",
            "plan.operators",
            "result.grain",
            "result.order_by",
            "lineage.entities",
            "lineage.fields",
        }:
            for index, actual_item in enumerate(actual):
                if isinstance(actual_item, (dict, list)):
                    errors.append(f"{path}[{index}] must be a scalar")
    elif isinstance(actual, (dict, list)):
        errors.append(f"{path} must be a scalar")
    return errors


def _resolve_weights(golden_suite: dict[str, Any]) -> dict[str, float]:
    declared = golden_suite.get("weights", DEFAULT_WEIGHTS)
    if not isinstance(declared, dict):
        raise ValueError("suite weights must be an object")
    expected_dimensions = set(DEFAULT_WEIGHTS)
    if set(declared) != expected_dimensions:
        raise ValueError(
            "suite weights must declare exactly: "
            + ", ".join(DEFAULT_WEIGHTS)
        )
    weights: dict[str, float] = {}
    for dimension in DEFAULT_WEIGHTS:
        value = declared[dimension]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"suite weight {dimension} must be a finite non-negative number"
            )
        try:
            converted = float(value)
        except (OverflowError, ValueError) as error:
            raise ValueError(
                f"suite weight {dimension} must be a finite non-negative number"
            ) from error
        if not math.isfinite(converted) or converted < 0:
            raise ValueError(
                f"suite weight {dimension} must be a finite non-negative number"
            )
        weights[dimension] = converted
    total = sum(weights.values())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("suite weights must have a finite positive total")
    return weights


def evaluate_case(
    golden: dict[str, Any],
    actual: Any,
    weights: dict[str, float] | None = None,
) -> CaseReport:
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
    if not isinstance(actual, dict):
        for dimension in DEFAULT_WEIGHTS:
            _difference(
                differences,
                dimension,
                f"cases.{case_id}",
                "candidate case object",
                actual,
                "INVALID_CANDIDATE_SHAPE: candidate case must be an object",
            )
        return CaseReport(
            case_id=case_id,
            score=0.0,
            dimension_scores={dimension: 0.0 for dimension in DEFAULT_WEIGHTS},
            differences=differences,
        )

    dimension_paths = {
        "semantic": "semantic",
        "time_range": "semantic",
        "member_normalization": "semantic",
        "plan": "plan",
        "result": "execution_result",
        "behavior": "governance",
        "governance": "governance",
        "lineage": "observability",
    }
    shape_checks: dict[str, list[bool]] = {
        dimension: [] for dimension in DEFAULT_WEIGHTS
    }
    for path, dimension in dimension_paths.items():
        if path not in actual:
            continue
        if path == "lineage":
            expected_value = {
                "entities": expected["semantic"].get("entities", []),
                "fields": expected["semantic"].get("fields", []),
            }
        else:
            expected_value = expected.get(path, {})
        errors = _container_type_errors(expected_value, actual[path], path)
        if errors:
            shape_checks[dimension].append(False)
        for error in errors:
            code = "INVALID_PLAN_SHAPE" if dimension == "plan" else "INVALID_CANDIDATE_SHAPE"
            _difference(
                differences,
                dimension,
                path,
                type(expected_value).__name__,
                actual[path],
                f"{code}: {error}",
            )

    actual_semantic = (
        actual.get("semantic", {}) if isinstance(actual.get("semantic"), dict) else {}
    )
    semantic_checks = shape_checks["semantic"]
    for key in ("entities", "fields", "metrics", "relations"):
        semantic_checks.append(
            _compare_exact(
                "semantic",
                f"semantic.{key}",
                expected["semantic"].get(key, []),
                actual_semantic.get(key, []),
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
    actual_plan_value = actual.get("plan", {})
    actual_plan = actual_plan_value if isinstance(actual_plan_value, dict) else {}
    plan_checks = shape_checks["plan"] + [
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
    plan_errors = validate_plan(actual_plan_value)
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
    actual_result = (
        actual.get("result", {}) if isinstance(actual.get("result"), dict) else {}
    )
    result_checks = shape_checks["execution_result"]
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
    tolerance = expected_result.get("tolerance", 0)
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

    governance_checks = shape_checks["governance"] + [
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
    actual_lineage_value = actual.get("lineage")
    actual_lineage = (
        actual_lineage_value if isinstance(actual_lineage_value, dict) else None
    )
    lineage_present = not lineage_required or bool(actual_lineage)
    observability_checks = shape_checks["observability"] + [lineage_present]
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
    active_weights = weights or DEFAULT_WEIGHTS
    total_weight = sum(active_weights.values())
    score = sum(
        dimension_scores[dimension] * weight / total_weight
        for dimension, weight in active_weights.items()
    )
    return CaseReport(
        case_id=case_id,
        score=round(score, 2),
        dimension_scores={key: round(value, 2) for key, value in dimension_scores.items()},
        differences=differences,
    )


def evaluate_bundle(
    golden_suite: Any,
    candidate_bundle: Any,
) -> EvaluationReport:
    raw_candidate_errors: list[str] = []
    if isinstance(candidate_bundle, dict):
        raw_cases = candidate_bundle.get("cases", {})
        if isinstance(raw_cases, list):
            seen_raw_ids: set[str] = set()
            for index, candidate in enumerate(raw_cases):
                if not isinstance(candidate, dict):
                    raw_candidate_errors.append(
                        f"candidate cases[{index}] must be an object"
                    )
                    continue
                candidate_id = candidate.get("id")
                if not isinstance(candidate_id, str) or not candidate_id.strip():
                    raw_candidate_errors.append(
                        f"candidate cases[{index}] must have a non-empty string ID"
                    )
                    continue
                if candidate_id in seen_raw_ids:
                    raw_candidate_errors.append(
                        f"candidate cases contains duplicate ID {candidate_id!r}"
                    )
                seen_raw_ids.add(candidate_id)
        elif isinstance(raw_cases, dict):
            if any(
                not isinstance(candidate_id, str) or not candidate_id.strip()
                for candidate_id in raw_cases
            ):
                raw_candidate_errors.append(
                    "candidate case mapping contains an empty or non-string ID"
                )

    golden_suite, golden_value_errors = _sanitize_json_value(
        golden_suite,
        "golden",
    )
    if golden_value_errors:
        raise ValueError(
            "golden suite contains non-JSON-compatible values: "
            + "; ".join(golden_value_errors)
        )
    if not isinstance(golden_suite, dict):
        raise ValueError("golden suite must be an object")
    golden_cases = golden_suite.get("cases", [])
    if not isinstance(golden_cases, list) or any(
        not isinstance(case, dict)
        or not isinstance(case.get("id"), str)
        or not case["id"].strip()
        for case in golden_cases
    ):
        raise ValueError("golden suite cases must be objects with non-empty string IDs")
    weights = _resolve_weights(golden_suite)
    candidate_bundle, value_errors = _sanitize_json_value(
        candidate_bundle,
        "candidate",
    )
    validation_errors = raw_candidate_errors + value_errors
    if not isinstance(candidate_bundle, dict):
        validation_errors.append("candidate bundle must be an object")
        raw_candidates: Any = {}
    else:
        raw_candidates = candidate_bundle.get("cases", {})

    candidates: dict[str, Any] = {}
    if isinstance(raw_candidates, list):
        seen: set[str] = set()
        valid_entries: list[tuple[str, dict[str, Any]]] = []
        for index, candidate in enumerate(raw_candidates):
            if not isinstance(candidate, dict):
                validation_errors.append(
                    f"candidate cases[{index}] must be an object"
                )
                continue
            candidate_id = candidate.get("id")
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                validation_errors.append(
                    f"candidate cases[{index}] must have a non-empty string ID"
                )
                continue
            if candidate_id in seen:
                validation_errors.append(
                    f"candidate cases contains duplicate ID {candidate_id!r}"
                )
                continue
            seen.add(candidate_id)
            valid_entries.append((candidate_id, candidate))
        if not validation_errors:
            candidates = dict(valid_entries)
    elif isinstance(raw_candidates, dict):
        invalid_ids = [
            candidate_id
            for candidate_id in raw_candidates
            if not isinstance(candidate_id, str) or not candidate_id.strip()
        ]
        if invalid_ids:
            validation_errors.append(
                "candidate case mapping contains an empty or non-string ID"
            )
        elif not validation_errors:
            candidates = raw_candidates
    else:
        validation_errors.append("candidate cases must be an object or list")

    validation_errors = list(dict.fromkeys(validation_errors))
    reports = [
        evaluate_case(case, candidates.get(case["id"]), weights)
        for case in golden_cases
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
    total_weight = sum(weights.values())
    score = sum(
        dimension_scores[dimension] * weight / total_weight
        for dimension, weight in weights.items()
    )
    return EvaluationReport(
        suite_version=golden_suite.get("suite_version", "unknown"),
        score=round(score, 2),
        dimension_scores=dimension_scores,
        cases=reports,
        validation_errors=validation_errors,
    )
