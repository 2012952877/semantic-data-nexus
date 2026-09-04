from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import request_for, wait_for_terminal
from pydantic import ValidationError

from semantic_backend.models import RunDetail, StartRunRequest

FIXTURES = Path(__file__).parents[1] / "contract-fixtures" / "v1"


def test_bff_start_fixture_round_trips_exactly() -> None:
    document = json.loads((FIXTURES / "bff-start-request.json").read_text(encoding="utf-8"))
    request = StartRunRequest.model_validate(document)
    assert request.model_dump(mode="json", by_alias=True) == document
    assert set(document) == {
        "runId",
        "clientRequestId",
        "workload",
        "question",
        "evaluationClock",
        "evaluationTimezone",
        "compilationMode",
        "executionMode",
        "outputMode",
        "requestedBy",
        "traceId",
    }


@pytest.mark.parametrize(
    "field",
    [
        "runId",
        "clientRequestId",
        "workload",
        "question",
        "evaluationClock",
        "evaluationTimezone",
        "compilationMode",
        "executionMode",
        "outputMode",
        "requestedBy",
        "traceId",
    ],
)
def test_bff_start_fixture_rejects_missing_required_fields(field: str) -> None:
    document = json.loads((FIXTURES / "bff-start-request.json").read_text(encoding="utf-8"))
    document.pop(field)
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate(document)


def test_bff_start_fixture_rejects_unknown_and_oversized_question() -> None:
    document = json.loads((FIXTURES / "bff-start-request.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate({**document, "unknownMember": True})
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate({**document, "question": "q" * 4_001})


def test_bff_start_accepts_utc_but_rejects_windows_timezone_id() -> None:
    document = json.loads((FIXTURES / "bff-start-request.json").read_text(encoding="utf-8"))
    StartRunRequest.model_validate({**document, "evaluationTimezone": "UTC"})
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate({**document, "evaluationTimezone": "Pacific Standard Time"})


def test_backend_detail_fixture_round_trips_exactly() -> None:
    document = json.loads((FIXTURES / "backend-run-detail.json").read_text(encoding="utf-8"))
    detail = RunDetail.model_validate(document)
    assert detail.model_dump(mode="json", by_alias=True) == document


@pytest.mark.parametrize(
    ("data_type", "value"),
    [
        ("integer", 9_007_199_254_740_992),
        ("float", 1.0000001e28),
        ("float", 9_223_372_036_854_775_808),
        ("timestamp", "2026-08-15T01:00:00"),
        ("timestamp", "2026-08-15X01:00:00Z"),
        ("date", "2026-W35-5"),
        ("integer", True),
    ],
)
def test_backend_detail_fixture_rejects_out_of_domain_scalars(
    data_type: str,
    value: object,
) -> None:
    document = json.loads((FIXTURES / "backend-run-detail.json").read_text(encoding="utf-8"))
    document["result"]["columns"][0]["dataType"] = data_type
    document["result"]["rows"][0][0] = value
    with pytest.raises(ValidationError):
        RunDetail.model_validate(document)


def test_backend_detail_preserves_small_finite_number() -> None:
    document = json.loads((FIXTURES / "backend-run-detail.json").read_text(encoding="utf-8"))
    document["result"]["columns"][1]["dataType"] = "float"
    document["result"]["rows"][0][1] = 1e-29
    detail = RunDetail.model_validate(document)
    assert detail.result is not None
    assert detail.result.rows[0][1] == 1e-29


@pytest.mark.parametrize(
    ("container", "field"),
    [
        ("result", "rowCount"),
        ("result", "truncated"),
        ("manifest", "rowCount"),
        ("manifest", "byteCount"),
    ],
)
def test_backend_detail_rejects_boolean_counts(
    container: str,
    field: str,
) -> None:
    document = json.loads((FIXTURES / "backend-run-detail.json").read_text(encoding="utf-8"))
    document[container][field] = True
    with pytest.raises(ValidationError):
        RunDetail.model_validate(document)


@pytest.mark.parametrize(
    "field",
    [
        "kind",
        "dataType",
        "format",
        "nullable",
        "truncated",
        "storage",
        "relation",
        "resultRowCount",
        "manifestRowCount",
        "byteCount",
    ],
)
def test_backend_detail_rejects_missing_governed_fields(field: str) -> None:
    document = json.loads((FIXTURES / "backend-run-detail.json").read_text(encoding="utf-8"))
    targets = {
        "kind": (document["physicalNodes"][0], "kind"),
        "dataType": (document["result"]["columns"][0], "dataType"),
        "format": (document["result"]["columns"][0], "format"),
        "nullable": (document["result"]["columns"][0], "nullable"),
        "truncated": (document["result"], "truncated"),
        "storage": (document["manifest"], "storage"),
        "relation": (document["lineage"]["edges"][0], "relation"),
        "resultRowCount": (document["result"], "rowCount"),
        "manifestRowCount": (document["manifest"], "rowCount"),
        "byteCount": (document["manifest"], "byteCount"),
    }
    target, key = targets[field]
    del target[key]
    with pytest.raises(ValidationError):
        RunDetail.model_validate(document)


@pytest.mark.parametrize("field", ["scope", "severity", "sequence"])
def test_backend_detail_rejects_missing_diagnostic_governance(field: str) -> None:
    document = json.loads((FIXTURES / "backend-run-detail.json").read_text(encoding="utf-8"))
    diagnostic = {
        "sequence": 0,
        "runId": document["runId"],
        "scope": "run",
        "scopeId": document["runId"],
        "code": "SYNTHETIC_INFO",
        "title": "Synthetic diagnostic",
        "message": "No action is required.",
        "recovery": "Continue with the synthetic workflow.",
        "severity": "info",
        "occurredAt": "2026-08-15T01:00:01Z",
    }
    del diagnostic[field]
    document["diagnostics"] = [diagnostic]
    with pytest.raises(ValidationError):
        RunDetail.model_validate(document)


async def test_actual_backend_detail_uses_cross_language_contract(service) -> None:
    request = request_for("run_00000000000000000000000000000081")
    await service.start(request)
    terminal = await wait_for_terminal(service, request.run_id)
    assert terminal.state.value == "Succeeded"
    detail = await service.get_detail(request.run_id)
    encoded = detail.model_dump_json(by_alias=True)
    validated = RunDetail.model_validate_json(encoded)
    assert validated.run_id == request.run_id
    assert validated.question == request.question
    assert validated.result is not None
    assert all(isinstance(row, list) for row in validated.result.rows)
