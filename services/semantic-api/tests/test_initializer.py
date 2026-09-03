from __future__ import annotations

from datetime import datetime

import pytest

from semantic_api.initializer import DeterministicInitializer
from semantic_api.models import (
    CompileStatus,
    InitializeRequest,
    MemberResolutionMode,
    ResolutionSource,
    ResolvedTermKind,
)
from semantic_api.ontology import OntologyRegistry


def request(question: str, **overrides: object) -> InitializeRequest:
    values: dict[str, object] = {
        "question": question,
        "evaluation_clock": datetime.fromisoformat("2026-08-15T09:00:00+08:00"),
        "evaluation_timezone": "Asia/Shanghai",
    }
    values.update(overrides)
    return InitializeRequest.model_validate(values)


def test_resolves_exact_synonym_and_member(registry: OntologyRegistry) -> None:
    result = DeterministicInitializer(registry).initialize(
        request("East regional profit"), "correlation"
    )

    resolved = {
        (term.kind, term.machine_id, term.resolution_source) for term in result.resolved_terms
    }
    assert (
        ResolvedTermKind.MEMBER,
        "region.east",
        ResolutionSource.LABEL,
    ) in resolved
    assert any(term.machine_id == "metric.profit" for term in result.resolved_terms)
    assert result.status is CompileStatus.SUCCEEDED


def test_exact_mode_does_not_use_synonyms(registry: OntologyRegistry) -> None:
    result = DeterministicInitializer(registry).initialize(
        request("Eastern earnings", member_resolution_mode=MemberResolutionMode.EXACT),
        "correlation",
    )

    assert result.resolved_terms == []


def test_ambiguous_member_requires_clarification(registry: OntologyRegistry) -> None:
    result = DeterministicInitializer(registry).initialize(
        request("Central regional profit"), "correlation"
    )

    assert result.status is CompileStatus.CLARIFICATION_REQUIRED
    diagnostic = next(item for item in result.diagnostics if item.code == "AMBIGUOUS_MEMBER")
    assert diagnostic.details["candidate_count"] == 2
    assert not any(term.kind is ResolvedTermKind.MEMBER for term in result.resolved_terms)


@pytest.mark.parametrize(
    ("phrase", "start", "end", "grain"),
    [
        ("今年", "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00", "year"),
        ("去年", "2025-01-01T00:00:00+08:00", "2026-01-01T00:00:00+08:00", "year"),
        ("上季度", "2026-04-01T00:00:00+08:00", "2026-07-01T00:00:00+08:00", "quarter"),
        ("本月", "2026-08-01T00:00:00+08:00", "2026-09-01T00:00:00+08:00", "month"),
        ("上月", "2026-07-01T00:00:00+08:00", "2026-08-01T00:00:00+08:00", "month"),
    ],
)
def test_fixed_clock_time_normalization(
    registry: OntologyRegistry, phrase: str, start: str, end: str, grain: str
) -> None:
    result = DeterministicInitializer(registry).initialize(request(phrase), "correlation")

    assert result.time_windows[0].start.isoformat() == start
    assert result.time_windows[0].end_exclusive.isoformat() == end
    assert result.time_windows[0].grain == grain
    assert result.time_windows[0].timezone == "Asia/Shanghai"


def test_invalid_timezone_is_explicit_failure(registry: OntologyRegistry) -> None:
    result = DeterministicInitializer(registry).initialize(
        request("今年", evaluation_timezone="Not/AZone"), "correlation"
    )

    assert result.status is CompileStatus.FAILED
    assert [item.code for item in result.diagnostics] == ["INVALID_TIMEZONE"]


def test_clock_requires_explicit_offset() -> None:
    with pytest.raises(ValueError, match="explicit UTC offset"):
        request(
            "今年",
            evaluation_clock=datetime.fromisoformat("2026-08-15T09:00:00"),
        )
