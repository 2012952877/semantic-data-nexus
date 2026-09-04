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


@pytest.mark.parametrize("field", ["runId", "question", "executionMode", "outputMode"])
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


def test_backend_detail_fixture_round_trips_exactly() -> None:
    document = json.loads((FIXTURES / "backend-run-detail.json").read_text(encoding="utf-8"))
    detail = RunDetail.model_validate(document)
    assert detail.model_dump(mode="json", by_alias=True) == document


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
