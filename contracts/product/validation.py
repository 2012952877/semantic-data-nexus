"""Offline product-contract conformance; never an authentication middleware."""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = Path(__file__).parent / "v1"
STANDARD_DOMAINS = frozenset(
    "ask runs statistics my-data ontologies knowledge-bases resolvers credentials "
    "llms prompt-templates compute-engines result-stores resolver-test users groups "
    "run-feedback api-logs replay api-reference help".split()
)
ASK_MODES = frozenset("single chat ontology-assistant clarification".split())
OPERATOR_FAMILIES = frozenset(
    "ACT AGGREGATE ASK DATE DEDUPLICATE DERIVE DISTINCT EXCEPT EXPLODE FILTER "
    "IMPUTE INTERSECT JOIN PICK PIVOT PROJECT RESAMPLE SAMPLE SEARCH SELECT SORT "
    "SUMMARIZE UNION_ALL UNION_DISTINCT UNPIVOT WINDOW".split()
)
REQUIRED_FOUNDATIONS = frozenset(
    "capability-registry trusted-context resource-version audit-envelope usage-envelope "
    "real-provider control-persistence identity-workspace catalog-compiler "
    "durable-runtime connector-conformance".split()
)
REQUIRED_COMMERCIAL = frozenset(
    "audit-metering quotas migrations restore failure-load secret-references license-sbom".split()
)
REQUIRED_INTEGRATIONS = frozenset(
    "openapi-json-schema oidc-oauth2 otel arrow-parquet openlineage".split()
)
DELIVERED = frozenset({"implemented", "integrated", "verified"})
FORMATS = FormatChecker()


@FORMATS.checks("uri", raises=ValueError)
def _uri(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        return False
    if re.search(r'[<>"{}|\\^`]|%(?![0-9a-fA-F]{2})', value):
        return False
    parsed = urlsplit(value)
    if not parsed.scheme:
        return False
    if parsed.scheme in {"http", "https"}:
        return bool(parsed.hostname) and (parsed.port is None or 0 < parsed.port <= 65535)
    return bool(parsed.path)


@FORMATS.checks("date-time", raises=ValueError)
def _date_time(value: object) -> bool:
    # jsonschema's date-time checker otherwise silently needs an optional dependency.
    if not isinstance(value, str):
        return True
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-5][0-9]:[0-5][0-9]"
        r"(?:\.[0-9]{1,6})?(?:[Zz]|[+-][0-9]{2}:[0-5][0-9])", value
    ):
        return False
    datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
    return True


def load_json(path: Path) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-finite JSON number: {value}")

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def validator(name: str) -> Draft202012Validator:
    schemas = [load_json(path) for path in sorted(SCHEMAS.glob("*.schema.json"))]
    registry: Registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas
    )
    schema = load_json(SCHEMAS / f"{name}.schema.json")
    return Draft202012Validator(
        schema, registry=registry, format_checker=FORMATS
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _unique_index(items: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    index = {item["id"]: item for item in items}
    _require(len(index) == len(items), f"Duplicate {label} ID")
    return index


def _check_ref(ref: str, root: Path, *, test: bool = False) -> None:
    path_text, *selectors = ref.split("::")
    path = PurePosixPath(path_text)
    _require(not path.is_absolute() and ".." not in path.parts, f"Unsafe reference: {ref}")
    resolved = root.joinpath(*path.parts).resolve()
    _require(resolved.is_relative_to(root.resolve()), f"Reference outside repository: {ref}")
    _require(resolved.is_file(), f"Missing reference: {ref}")
    if test:
        _require(
            len(selectors) == 1 and selectors[0].startswith("test_")
            and resolved.suffix == ".py" and resolved.name.startswith("test_"),
            f"Expected executable pytest reference: {ref}",
        )
    if selectors:
        _require(resolved.suffix == ".py", f"Unsupported selector: {ref}")
        tree = ast.parse(resolved.read_text(encoding="utf-8"))
        # Only module-level pytest functions qualify; a nested helper is not collected.
        names = {
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        _require(len(selectors) == 1 and selectors[0] in names, f"Missing selector: {ref}")


def validate_registry(data: dict[str, Any], root: Path = ROOT) -> None:
    validator("capability-registry").validate(data)
    sources = _unique_index(data["sources"], "source")
    evidence = _unique_index(data["evidence"], "evidence")
    capabilities = _unique_index(data["capabilities"], "capability")
    coverage = {
        "standard-domain": {f"standard.{name}" for name in STANDARD_DOMAINS},
        "ask-mode": {f"ask.{name}" for name in ASK_MODES},
        "operator": {
            f"operator.{name.lower().replace('_', '-')}" for name in OPERATOR_FAMILIES
        },
        "foundation": {f"foundation.{name}" for name in REQUIRED_FOUNDATIONS},
        "commercial": {f"commercial.{name}" for name in REQUIRED_COMMERCIAL},
        "integration": {f"integration.{name}" for name in REQUIRED_INTEGRATIONS},
    }
    for category, expected in coverage.items():
        actual = {item["id"] for item in capabilities.values() if item["category"] == category}
        _require(expected <= actual, f"Missing {category} coverage: {sorted(expected - actual)}")
    for item in evidence.values():
        _check_ref(item["ref"], root, test=item["kind"] == "test")
        if item["kind"] in {"integration", "verification"}:
            report = root.joinpath(*PurePosixPath(item["ref"]).parts)
            _require(report.suffix == ".json", "Run evidence must reference a JSON record")
            _require(load_json(report) == item["run"], "Run evidence record mismatch")
    for item in capabilities.values():
        capability_id = item["id"]
        source = item["observed_source"]
        _require(source["ref"] in sources, f"Unknown source for {capability_id}")
        source_kind = sources[source["ref"]]["kind"]
        if source_kind == "product-requirement":
            _require(source["confidence"] == "not-observed", "Requirement is not observation")
        if item["category"] in {"standard-domain", "ask-mode", "operator"}:
            _require(source_kind == "sanitized-observation", "Observed inventory needs observation")
        expected_milestone = "M1" if item["issue"] <= 33 else "M2" if item["issue"] <= 37 else "M3"
        _require(item["milestone"] == expected_milestone, f"Wrong issue milestone: {capability_id}")
        for dependency in item["dependencies"]:
            _require(dependency in capabilities, f"Unknown dependency: {dependency}")
        for ref in item["evidence"]:
            _require(ref in evidence, f"Unknown evidence: {ref}")
        for case in item["acceptance"].values():
            if case["test_ref"] is not None:
                _check_ref(case["test_ref"], root, test=True)
        state = item["state"]
        if state in DELIVERED:
            kinds = {evidence[ref]["kind"] for ref in item["evidence"]}
            required = {"implementation", "test"}
            if state in {"integrated", "verified"}:
                required.add("integration")
            if state == "verified":
                required.add("verification")
            _require(required <= kinds, f"Missing delivered evidence: {capability_id}")
            for case_kind in ("positive", "negative"):
                _require(
                    item["acceptance"][case_kind]["test_ref"] is not None,
                    f"Missing delivered acceptance: {capability_id}.{case_kind}",
                )
            if state in {"integrated", "verified"}:
                _require(
                    item["acceptance"]["integration"]["test_ref"] is not None,
                    f"Missing integration acceptance: {capability_id}",
                )
            _require(
                all(capabilities[dep]["state"] in DELIVERED for dep in item["dependencies"]),
                f"Undelivered dependency of {capability_id}",
            )
    _require(
        capabilities["operator.act"].get("execution_policy")
        == {"tool_authorization": "explicit", "implicit_writes": "deny"},
        "ACT requires explicit tool authorization and must deny implicit writes",
    )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(capability_id: str) -> None:
        _require(capability_id not in visiting, f"Dependency cycle at {capability_id}")
        if capability_id in visited:
            return
        visiting.add(capability_id)
        for dependency in capabilities[capability_id]["dependencies"]:
            visit(dependency)
        visiting.remove(capability_id)
        visited.add(capability_id)

    for capability_id in capabilities:
        visit(capability_id)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.upper().replace("Z", "+00:00"))


def validate_context(data: dict[str, Any], *, now: datetime) -> None:
    """Check shape/time consistency, NOT signatures, membership or permission authority."""
    validator("trusted-context").validate(data)
    _require(now.utcoffset() is not None, "Validation time must include a timezone")
    authentication = data["authentication"]
    authenticated = _timestamp(authentication["authenticated_at"])
    authorized = _timestamp(data["membership"]["authorized_at"])
    expires = _timestamp(authentication["expires_at"])
    _require(authenticated <= authorized <= now < expires, "Invalid or expired context interval")


def validate_append_sequence(events: list[dict[str, Any]]) -> None:
    """Offline full-stream conformance, not a concurrent or durable append implementation."""
    _require(bool(events), "Expected a nonempty full stream")
    seen: dict[str, dict[str, Any]] = {}
    first = events[0]
    version = first.get("contract_version")
    _require(version in {"audit-envelope/v1", "usage-envelope/v1"}, "Unknown envelope version")
    envelope_validator = validator(version.split("/")[0])
    previous: dict[str, Any] | None = None
    for sequence, event in enumerate(events, start=1):
        envelope_validator.validate(event)
        _require(event["scope"] == first["scope"], "Mixed workspace scope")
        _require(event["stream_id"] == first["stream_id"], "Mixed stream identity")
        _require(event["event_id"] not in seen, "Duplicate event identity")
        _require(event["sequence"] == sequence, "Noncontiguous append sequence")
        previous_id = previous["event_id"] if previous else None
        _require(event["previous_event_id"] == previous_id, "Invalid append ancestry")
        if previous:
            _require(
                _timestamp(event["recorded_at"]) >= _timestamp(previous["recorded_at"]),
                "Recorded time moved backwards",
            )
        if version == "audit-envelope/v1":
            _require(event["resource"]["scope"] == event["scope"], "Foreign resource scope")
        elif event["kind"] == "adjustment":
            target = seen.get(event["corrects_event_id"])
            _require(target is not None, "Correction must reference an earlier measurement")
            assert target is not None
            _require(target["kind"] == "measurement", "Cannot correct an adjustment")
            _require(
                all(event[key] == target[key] for key in ("meter", "unit", "run_id", "producer")),
                "Correction measurement identity mismatch",
            )
        seen[event["event_id"]] = event
        previous = event
