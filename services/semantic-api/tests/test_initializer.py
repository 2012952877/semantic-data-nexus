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


def test_long_exact_member_suppresses_overlapping_short_synonym(
    registry: OntologyRegistry,
) -> None:
    result = DeterministicInitializer(registry).initialize(
        request("Central North regional profit"), "correlation"
    )

    assert result.status is CompileStatus.SUCCEEDED
    assert not any(item.code == "AMBIGUOUS_MEMBER" for item in result.diagnostics)
    assert any(
        term.kind is ResolvedTermKind.MEMBER and term.machine_id == "region.central_north"
        for term in result.resolved_terms
    )


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


@pytest.mark.parametrize("timezone_name", ["/etc/passwd", "..\\zone", "a?b"])
def test_malformed_timezone_key_is_explicit_failure(
    registry: OntologyRegistry, timezone_name: str
) -> None:
    result = DeterministicInitializer(registry).initialize(
        request("今年", evaluation_timezone=timezone_name), "correlation"
    )

    assert result.status is CompileStatus.FAILED
    assert any(item.code == "INVALID_TIMEZONE" for item in result.diagnostics)


def test_multiple_relative_windows_are_explicitly_rejected(
    registry: OntologyRegistry,
) -> None:
    result = DeterministicInitializer(registry).initialize(
        request("Compare 今年 and 去年"), "correlation"
    )

    assert result.status is CompileStatus.FAILED
    assert any(item.code == "MULTIPLE_TIME_WINDOWS_UNSUPPORTED" for item in result.diagnostics)


@pytest.mark.parametrize(
    ("question", "code"),
    [
        ("Show regional profit but exclude East", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit but do not include East", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit not including East", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East excluded", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East is excluded", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East isn't included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East not included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East wouldn't be included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East needn't be included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East hasn't been included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East has not been included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East hasn\u2019t been included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East shouldn't have been included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East should not have been included", "NEGATED_MEMBER_UNSUPPORTED"),
        (
            "Show regional profit East shouldn\u2019t have been included",
            "NEGATED_MEMBER_UNSUPPORTED",
        ),
        ("Show regional profit East cannot have been included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East can't have been included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East can\u2019t have been included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East won't have been included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East won\u2019t have been included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East ought not to have been included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit East oughtn't to have been included", "NEGATED_MEMBER_UNSUPPORTED"),
        (
            "Show regional profit East oughtn\u2019t to have been included",
            "NEGATED_MEMBER_UNSUPPORTED",
        ),
        ("Show regional profit for East, not included", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit for East (omitted)", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit shouldn't include East", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit don't show East", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit don\u2019t include East", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit 华东被排除", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit except for East", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit except the East", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit other than East", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit 除了 East", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit 非华东", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit 华东以外", "NEGATED_MEMBER_UNSUPPORTED"),
        ("Show regional profit but not 去年", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit but 不要包括去年", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 除了去年", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年以外", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit not including 去年", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年 excluded", "NEGATED_TIME_UNSUPPORTED"),
        ("Compare 去年 sales with 去年 excluded", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年应该被排除", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年 wasn't included", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年 not included", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年 couldn't be shown", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年 hasn't been included", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年 hasn\u2019t been shown", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年 should not have been included", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年 cannot have been shown", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年 can\u2019t have been included", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年 shan't have been shown", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年 shan\u2019t have been shown", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年 oughtn't to have been shown", "NEGATED_TIME_UNSUPPORTED"),
        ("Show regional profit 去年 is not shown", "NEGATED_TIME_UNSUPPORTED"),
    ],
)
def test_negated_constraints_are_explicitly_rejected(
    registry: OntologyRegistry, question: str, code: str
) -> None:
    result = DeterministicInitializer(registry).initialize(request(question), "correlation")

    assert result.status is CompileStatus.FAILED
    assert any(item.code == code for item in result.diagnostics)


@pytest.mark.parametrize(
    ("question", "kind"),
    [
        ("do not use sales", ResolvedTermKind.ENTITY),
        ("exclude region", ResolvedTermKind.FIELD),
        ("without profit", ResolvedTermKind.METRIC),
        ("profit excluded", ResolvedTermKind.METRIC),
        ("profit should be excluded", ResolvedTermKind.METRIC),
        ("profit shouldn\u2019t be included", ResolvedTermKind.METRIC),
        ("profit not used", ResolvedTermKind.METRIC),
        ("profit mightn't be shown", ResolvedTermKind.METRIC),
        ("profit oughtn't to be shown", ResolvedTermKind.METRIC),
        ("profit hasn't been used", ResolvedTermKind.METRIC),
        ("profit hasn\u2019t been shown", ResolvedTermKind.METRIC),
        ("profit shouldn't have been included", ResolvedTermKind.METRIC),
        ("profit should not have been used", ResolvedTermKind.METRIC),
        ("profit cannot have been used", ResolvedTermKind.METRIC),
        ("profit can\u2019t have been shown", ResolvedTermKind.METRIC),
        ("profit ought not to have been included", ResolvedTermKind.METRIC),
        ("profit oughtn\u2019t to have been shown", ResolvedTermKind.METRIC),
        ("profit is not shown", ResolvedTermKind.METRIC),
        ("利润应排除", ResolvedTermKind.METRIC),
    ],
)
def test_negated_semantic_concepts_are_explicitly_rejected(
    registry: OntologyRegistry,
    question: str,
    kind: ResolvedTermKind,
) -> None:
    result = DeterministicInitializer(registry).initialize(request(question), "correlation")

    assert result.status is CompileStatus.FAILED
    assert any(item.code == "NEGATED_CONCEPT_UNSUPPORTED" for item in result.diagnostics)
    assert not any(term.kind is kind for term in result.resolved_terms)


def test_clock_requires_explicit_offset() -> None:
    with pytest.raises(ValueError, match="explicit UTC offset"):
        request(
            "今年",
            evaluation_clock=datetime.fromisoformat("2026-08-15T09:00:00"),
        )


def test_clock_outside_timezone_range_is_explicit_failure(
    registry: OntologyRegistry,
) -> None:
    result = DeterministicInitializer(registry).initialize(
        request(
            "今年",
            evaluation_clock=datetime.fromisoformat("0001-01-01T00:00:00+14:00"),
            evaluation_timezone="Etc/GMT+12",
        ),
        "correlation",
    )

    assert result.status is CompileStatus.FAILED
    assert any(item.code == "INVALID_EVALUATION_CLOCK" for item in result.diagnostics)
