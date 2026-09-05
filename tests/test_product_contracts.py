from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from contracts.product.validation import (
    ROOT,
    SCHEMAS,
    load_json,
    validate_append_sequence,
    validate_context,
    validate_registry,
    validator,
)

EXAMPLES = load_json(SCHEMAS / "examples.json")
POSITIVE = {item["id"]: item for item in EXAMPLES["positive"]}


def test_product_schemas_are_valid() -> None:
    schemas = sorted(SCHEMAS.glob("*.schema.json"))
    assert len(schemas) == 6
    for path in schemas:
        Draft202012Validator.check_schema(load_json(path))


@pytest.mark.parametrize("example", EXAMPLES["positive"], ids=lambda item: item["id"])
def test_positive_examples(example: dict) -> None:
    validator(example["schema"]).validate(example["document"])


@pytest.mark.parametrize("example", EXAMPLES["negative"], ids=lambda item: item["id"])
def test_negative_examples(example: dict) -> None:
    base = POSITIVE[example["base"]]
    document = copy.deepcopy(base["document"])
    target = document
    for key in example["path"][:-1]:
        target = target[key]
    key = example["path"][-1]
    if example["operation"] == "remove":
        del target[key]
    else:
        assert example["operation"] == "set"
        target[key] = example["value"]
    with pytest.raises(ValidationError):
        validator(base["schema"]).validate(document)


def test_capability_registry() -> None:
    validate_registry(load_json(SCHEMAS / "capabilities.json"))


def test_schema_requires_act_authorization_policy() -> None:
    data = load_json(SCHEMAS / "capabilities.json")
    act = next(item for item in data["capabilities"] if item["id"] == "operator.act")
    del act["execution_policy"]
    with pytest.raises(ValidationError):
        validator("capability-registry").validate(data)


@pytest.mark.parametrize("example", EXAMPLES["positive"], ids=lambda item: item["id"])
def test_required_fields_and_unknown_properties(example: dict) -> None:
    schema = load_json(SCHEMAS / f"{example['schema']}.schema.json")
    contract_validator = validator(example["schema"])
    for key in schema["required"]:
        document = copy.deepcopy(example["document"])
        del document[key]
        with pytest.raises(ValidationError):
            contract_validator.validate(document)
    with pytest.raises(ValidationError):
        contract_validator.validate({**example["document"], "unexpected": "synthetic"})


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-01-01", "2026-01-01T00:00:00", "2026-01-01T00:00:00+00:60",
        "2026-01-01T00:00:00+24:00", "2026-01-01T00:00:60Z",
        "2026-01-01T00:00:00.1234567Z", "2026-01-01T00:00:00Z\n",
        "2026-02-30T00:00:00Z",
    ],
)
def test_timestamp_profile(timestamp: str) -> None:
    document = copy.deepcopy(POSITIVE["trusted-context"]["document"])
    document["membership"]["authorized_at"] = timestamp
    with pytest.raises(ValidationError):
        validator("trusted-context").validate(document)


@pytest.mark.parametrize(
    "issuer",
    [
        "https://", "https://[invalid", "https://issuer.example.invalid/%zz",
        "https://issuer.example.invalid:invalid", "https://user@issuer.example.invalid",
        "https://issuer.example.invalid?scope=synthetic",
        "https://issuer.example.invalid#synthetic",
        "https://issuer.example.invalid\\synthetic",
        "https://issuer.example.invalid/<synthetic>",
    ],
)
def test_issuer_uri_profile(issuer: str) -> None:
    document = copy.deepcopy(POSITIVE["trusted-context"]["document"])
    document["principal"]["issuer"] = issuer
    with pytest.raises(ValidationError):
        validator("trusted-context").validate(document)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-domain", "missing-mode", "missing-operator", "missing-commercial",
        "missing-integration", "duplicate-id", "duplicate-source", "duplicate-evidence",
        "dangling-dependency", "cycle", "self-cycle", "unknown-source", "unknown-evidence",
        "delivered-without-evidence", "delivered-without-test", "integrated-without-evidence",
        "verified-without-evidence", "missing-test-path", "missing-test-selector",
        "non-test-selector", "unsafe-evidence-path", "observation-overclaim",
        "wrong-milestone", "missing-act-policy", "implicit-act-writes",
        "undelivered-dependency",
    ],
)
def test_invalid_registry(mutation: str) -> None:
    data = load_json(SCHEMAS / "capabilities.json")
    capabilities = {item["id"]: item for item in data["capabilities"]}
    first = capabilities["standard.ask"]
    delivered = capabilities["foundation.capability-registry"]
    removals = {
        "missing-domain": "standard.help",
        "missing-mode": "ask.chat",
        "missing-operator": "operator.window",
        "missing-commercial": "commercial.quotas",
        "missing-integration": "integration.otel",
    }
    if mutation in removals:
        data["capabilities"].remove(capabilities[removals[mutation]])
    elif mutation == "duplicate-id":
        data["capabilities"].append(copy.deepcopy(first))
    elif mutation == "duplicate-source":
        data["sources"].append(copy.deepcopy(data["sources"][0]))
    elif mutation == "duplicate-evidence":
        data["evidence"].append(copy.deepcopy(data["evidence"][0]))
    elif mutation == "dangling-dependency":
        first["dependencies"].append("missing.capability")
    elif mutation == "cycle":
        capabilities["ask.single"]["dependencies"].append("standard.ask")
    elif mutation == "self-cycle":
        first["dependencies"].append(first["id"])
    elif mutation == "unknown-source":
        first["observed_source"]["ref"] = "missing.source"
    elif mutation == "unknown-evidence":
        first["evidence"].append("missing.evidence")
    elif mutation == "delivered-without-evidence":
        first["state"] = "implemented"
    elif mutation == "delivered-without-test":
        delivered["acceptance"]["negative"]["test_ref"] = None
    elif mutation == "integrated-without-evidence":
        delivered["state"] = "integrated"
    elif mutation == "verified-without-evidence":
        delivered["state"] = "verified"
    elif mutation == "missing-test-path":
        delivered["acceptance"]["positive"]["test_ref"] = "tests/missing.py::test_missing"
    elif mutation == "missing-test-selector":
        delivered["acceptance"]["positive"]["test_ref"] = (
            "tests/test_product_contracts.py::test_missing"
        )
    elif mutation == "non-test-selector":
        delivered["acceptance"]["positive"]["test_ref"] = (
            "contracts/product/validation.py::validate_registry"
        )
    elif mutation == "unsafe-evidence-path":
        data["evidence"][0]["ref"] = "../outside.py"
    elif mutation == "observation-overclaim":
        delivered["observed_source"]["confidence"] = "high"
    elif mutation == "wrong-milestone":
        first["milestone"] = "M3"
    elif mutation == "missing-act-policy":
        del capabilities["operator.act"]["execution_policy"]
    elif mutation == "implicit-act-writes":
        capabilities["operator.act"]["execution_policy"]["implicit_writes"] = "allow"
    elif mutation == "undelivered-dependency":
        delivered["dependencies"].append("standard.ask")
    else:
        pytest.fail(f"Unhandled mutation: {mutation}")
    with pytest.raises((ValueError, ValidationError)):
        validate_registry(data)


def test_positive_delivered_states_and_evidence(tmp_path: Path) -> None:
    """Exercise promotion gates without pretending this synthetic evidence is real delivery."""
    data = load_json(SCHEMAS / "capabilities.json")
    target = next(
        item for item in data["capabilities"]
        if item["id"] == "foundation.capability-registry"
    )
    refs = {item["ref"] for item in data["evidence"]}
    refs.update(
        case["test_ref"]
        for item in data["capabilities"] for case in item["acceptance"].values()
        if case["test_ref"] is not None
    )
    for ref in refs:
        path = Path(ref.split("::")[0])
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, destination)
    run = {
        "commit": "a" * 40, "command": "synthetic-gate-test",
        "environment": "synthetic-not-release-evidence", "result": "passed",
        "recorded_at": "2026-01-01T00:00:00Z",
    }
    report = tmp_path / "synthetic-run.json"
    report.write_text(json.dumps(run), encoding="utf-8")
    for kind in ("integration", "verification"):
        data["evidence"].append({
            "id": f"synthetic.{kind}", "kind": kind,
            "ref": "synthetic-run.json",
            "scope": "Synthetic test of promotion gates only.", "run": run,
        })
        target["evidence"].append(f"synthetic.{kind}")
    target["acceptance"]["integration"]["test_ref"] = (
        "tests/test_product_contracts.py::test_capability_registry"
    )
    for state in ("implemented", "integrated", "verified"):
        target["state"] = state
        validate_registry(data, root=tmp_path)
    target["acceptance"]["integration"]["test_ref"] = None
    with pytest.raises(ValueError, match="Missing integration acceptance"):
        validate_registry(data, root=tmp_path)
    target["acceptance"]["integration"]["test_ref"] = (
        "tests/test_product_contracts.py::test_capability_registry"
    )
    report.write_text(json.dumps({**run, "result": "failed"}), encoding="utf-8")
    with pytest.raises(ValueError, match="Run evidence record mismatch"):
        validate_registry(data, root=tmp_path)
    data["evidence"][-1].pop("run")
    with pytest.raises(ValidationError):
        validate_registry(data, root=tmp_path)


def test_context_temporal_consistency() -> None:
    context = copy.deepcopy(POSITIVE["trusted-context"]["document"])
    validate_context(context, now=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc))
    for now in (
        datetime(2025, 12, 31, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 2),
    ):
        with pytest.raises(ValueError):
            validate_context(context, now=now)
    context["membership"]["authorized_at"] = "2025-12-31T23:59:00Z"
    with pytest.raises(ValueError):
        validate_context(context, now=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc))


def test_secret_reference_shape() -> None:
    common = validator("common")
    schema = {"$ref": "urn:semantic-data-nexus:product:common:v1#/$defs/secretReference"}
    reference_validator = common.evolve(schema=schema)
    reference = {"store": "synthetic-store", "name": "synthetic-reference", "version": "v1"}
    reference_validator.validate(reference)
    for invalid in (
        {**reference, "value": "synthetic-not-a-secret"},
        {**reference, "name": "../synthetic-reference"},
        {**reference, "version": ""},
        {**reference, "store": "https://example.invalid"},
    ):
        with pytest.raises(ValidationError):
            reference_validator.validate(invalid)


def test_append_sequences() -> None:
    audit = copy.deepcopy(POSITIVE["audit-envelope"]["document"])
    next_audit = {
        **audit, "event_id": "synthetic-audit-2", "sequence": 2,
        "previous_event_id": audit["event_id"],
    }
    validate_append_sequence([audit, next_audit])
    usage = copy.deepcopy(POSITIVE["usage-envelope"]["document"])
    adjustment = copy.deepcopy(POSITIVE["usage-adjustment"]["document"])
    validate_append_sequence([usage, adjustment])


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate", "gap", "ancestry", "workspace", "tenant", "stream", "time",
        "unknown-correction", "correction-unit", "correction-meter", "correction-run",
        "correction-producer", "correction-of-adjustment", "resource-scope", "empty",
        "mixed-envelope",
    ],
)
def test_invalid_append_sequences(mutation: str) -> None:
    events = [
        copy.deepcopy(POSITIVE["usage-envelope"]["document"]),
        copy.deepcopy(POSITIVE["usage-adjustment"]["document"]),
    ]
    first, second = events
    if mutation == "duplicate":
        second["event_id"] = first["event_id"]
    elif mutation == "gap":
        second["sequence"] = 3
    elif mutation == "ancestry":
        second["previous_event_id"] = "synthetic-other"
    elif mutation in {"workspace", "tenant"}:
        second["scope"][f"{mutation}_id"] = "synthetic-other"
    elif mutation == "stream":
        second["stream_id"] = "synthetic-other"
    elif mutation == "time":
        second["recorded_at"] = "2025-12-31T23:59:00Z"
    elif mutation == "unknown-correction":
        second["corrects_event_id"] = "synthetic-other"
    elif mutation.startswith("correction-") and mutation != "correction-of-adjustment":
        field = mutation.removeprefix("correction-")
        field = "run_id" if field == "run" else field
        second[field] = "bytes" if field == "unit" else "synthetic-other"
    elif mutation == "correction-of-adjustment":
        events.append({
            **second, "event_id": "synthetic-usage-3", "sequence": 3,
            "previous_event_id": second["event_id"],
            "corrects_event_id": second["event_id"],
        })
    elif mutation == "resource-scope":
        events = [copy.deepcopy(POSITIVE["audit-envelope"]["document"])]
        events[0]["resource"]["scope"]["workspace_id"] = "synthetic-other"
    elif mutation == "empty":
        events = []
    elif mutation == "mixed-envelope":
        events[1] = copy.deepcopy(POSITIVE["audit-envelope"]["document"])
    else:
        pytest.fail(f"Unhandled mutation: {mutation}")
    with pytest.raises((ValueError, ValidationError)):
        validate_append_sequence(events)


def test_product_loader_rejects_nonfinite_json(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-invalid.json"
    for constant in ("NaN", "Infinity", "-Infinity"):
        path.write_text(f'{{"quantity": {constant}}}', encoding="utf-8")
        with pytest.raises(ValueError, match="Non-finite"):
            load_json(path)
