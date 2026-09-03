from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from semantic_api.models import (
    CompileStatus,
    Diagnostic,
    DiagnosticSeverity,
    DiagnosticStage,
    InitializeRequest,
    InitializeResponse,
    MemberResolutionMode,
    ResolutionSource,
    ResolvedTerm,
    ResolvedTermKind,
    TimeWindow,
)
from semantic_api.ontology import OntologyRegistry

type TimeGrain = Literal["year", "quarter", "month"]


@dataclass(frozen=True)
class _ResolutionCandidate:
    source_text: str
    kind: ResolvedTermKind
    machine_id: str
    source: ResolutionSource


@dataclass(frozen=True)
class _MemberMatch:
    candidate: _ResolutionCandidate
    start: int
    end: int


class DeterministicInitializer:
    def __init__(self, registry: OntologyRegistry) -> None:
        self.registry = registry

    def initialize(self, request: InitializeRequest, correlation_id: str) -> InitializeResponse:
        terms, diagnostics = self._resolve_terms(request.question, request.member_resolution_mode)
        windows, time_diagnostics = self._normalize_time(
            request.question,
            request.evaluation_clock,
            request.evaluation_timezone,
        )
        diagnostics.extend(time_diagnostics)
        if len(windows) > 1:
            diagnostics.append(
                Diagnostic(
                    code="MULTIPLE_TIME_WINDOWS_UNSUPPORTED",
                    severity=DiagnosticSeverity.ERROR,
                    stage=DiagnosticStage.INITIALIZE,
                    message="This compiler version supports one relative time window per request.",
                    path="question",
                    details={"window_count": len(windows)},
                )
            )
        for window in windows:
            terms.append(
                ResolvedTerm(
                    source_text=window.source_text,
                    kind=ResolvedTermKind.TIME_WINDOW,
                    machine_id=(
                        f"time:{window.start.isoformat()}/{window.end_exclusive.isoformat()}"
                    ),
                    resolution_source=ResolutionSource.RELATIVE_TIME,
                )
            )
        context = self.registry.retrieve(request.question, terms, request.ontology_scope)
        status = (
            CompileStatus.CLARIFICATION_REQUIRED
            if any(item.code == "AMBIGUOUS_MEMBER" for item in diagnostics)
            else (
                CompileStatus.FAILED
                if any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics)
                else CompileStatus.SUCCEEDED
            )
        )
        return InitializeResponse(
            status=status,
            correlation_id=correlation_id,
            resolved_terms=terms,
            time_windows=windows,
            selected_semantic_context=context,
            diagnostics=diagnostics,
        )

    def _resolve_terms(
        self, question: str, mode: MemberResolutionMode
    ) -> tuple[list[ResolvedTerm], list[Diagnostic]]:
        question_folded = question.casefold()
        candidates: list[_ResolutionCandidate] = []

        for entity in self.registry.document.entities:
            candidates.extend(
                self._concept_candidates(
                    question_folded,
                    entity.id,
                    entity.label,
                    entity.synonyms,
                    ResolvedTermKind.ENTITY,
                    mode,
                )
            )
        for field in self.registry.document.fields:
            candidates.extend(
                self._concept_candidates(
                    question_folded,
                    field.id,
                    field.label,
                    field.synonyms,
                    ResolvedTermKind.FIELD,
                    mode,
                )
            )
        for metric in self.registry.document.metrics:
            candidates.extend(
                self._concept_candidates(
                    question_folded,
                    metric.id,
                    metric.label,
                    metric.synonyms,
                    ResolvedTermKind.METRIC,
                    mode,
                )
            )

        member_matches: list[_MemberMatch] = []
        for field in self.registry.document.fields:
            for member in field.members:
                for source_text, source, start, end in self._match_form_spans(
                    question_folded,
                    member.id,
                    member.label,
                    member.synonyms,
                    mode,
                ):
                    member_matches.append(
                        _MemberMatch(
                            candidate=_ResolutionCandidate(
                                source_text,
                                ResolvedTermKind.MEMBER,
                                member.id,
                                source,
                            ),
                            start=start,
                            end=end,
                        )
                    )

        diagnostics: list[Diagnostic] = []
        for matches in self._preferred_member_matches(member_matches):
            if self._is_negated(question_folded, matches[0].start, matches[0].end):
                diagnostics.append(
                    Diagnostic(
                        code="NEGATED_MEMBER_UNSUPPORTED",
                        severity=DiagnosticSeverity.ERROR,
                        stage=DiagnosticStage.INITIALIZE,
                        message="Negative member constraints are not supported in this version.",
                        path="question",
                        details={"mention": matches[0].candidate.source_text.casefold()},
                    )
                )
                continue
            machine_ids = sorted({match.candidate.machine_id for match in matches})
            if len(machine_ids) > 1:
                diagnostics.append(
                    Diagnostic(
                        code="AMBIGUOUS_MEMBER",
                        severity=DiagnosticSeverity.ERROR,
                        stage=DiagnosticStage.INITIALIZE,
                        message="A member mention matches multiple governed members.",
                        path="question",
                        details={
                            "mention": matches[0].candidate.source_text.casefold(),
                            "candidate_count": len(machine_ids),
                            "candidates": ",".join(machine_ids),
                        },
                    )
                )
                continue
            candidates.append(matches[0].candidate)

        unique: dict[tuple[ResolvedTermKind, str], _ResolutionCandidate] = {}
        for candidate in candidates:
            key = (candidate.kind, candidate.machine_id)
            existing = unique.get(key)
            if existing is None or self._source_rank(candidate.source) < self._source_rank(
                existing.source
            ):
                unique[key] = candidate
        resolved = [
            ResolvedTerm(
                source_text=item.source_text,
                kind=item.kind,
                machine_id=item.machine_id,
                resolution_source=item.source,
            )
            for item in sorted(unique.values(), key=lambda item: (item.kind, item.machine_id))
        ]
        return resolved, diagnostics

    @staticmethod
    def _source_rank(source: ResolutionSource) -> int:
        return {
            ResolutionSource.MACHINE_ID: 0,
            ResolutionSource.LABEL: 1,
            ResolutionSource.SYNONYM: 2,
            ResolutionSource.RELATIVE_TIME: 3,
        }[source]

    def _concept_candidates(
        self,
        question: str,
        machine_id: str,
        label: str,
        synonyms: list[str],
        kind: ResolvedTermKind,
        mode: MemberResolutionMode,
    ) -> list[_ResolutionCandidate]:
        return [
            _ResolutionCandidate(source_text, kind, machine_id, source)
            for source_text, source in self._match_forms(
                question, machine_id, label, synonyms, mode
            )
        ]

    @staticmethod
    def _match_forms(
        question: str,
        machine_id: str,
        label: str,
        synonyms: list[str],
        mode: MemberResolutionMode,
    ) -> list[tuple[str, ResolutionSource]]:
        forms: list[tuple[str, ResolutionSource]] = [
            (machine_id, ResolutionSource.MACHINE_ID),
            (label, ResolutionSource.LABEL),
        ]
        if mode is MemberResolutionMode.EXACT_AND_SYNONYM:
            forms.extend((item, ResolutionSource.SYNONYM) for item in synonyms)
        return [
            (text, source)
            for text, source in forms
            if text and DeterministicInitializer._contains_mention(question, text)
        ]

    @classmethod
    def _match_form_spans(
        cls,
        question: str,
        machine_id: str,
        label: str,
        synonyms: list[str],
        mode: MemberResolutionMode,
    ) -> list[tuple[str, ResolutionSource, int, int]]:
        forms: list[tuple[str, ResolutionSource]] = [
            (machine_id, ResolutionSource.MACHINE_ID),
            (label, ResolutionSource.LABEL),
        ]
        if mode is MemberResolutionMode.EXACT_AND_SYNONYM:
            forms.extend((item, ResolutionSource.SYNONYM) for item in synonyms)
        matches: list[tuple[str, ResolutionSource, int, int]] = []
        for text, source in forms:
            if not text:
                continue
            matches.extend(
                (text, source, start, end) for start, end in cls._mention_spans(question, text)
            )
        return matches

    @classmethod
    def _preferred_member_matches(cls, matches: list[_MemberMatch]) -> list[list[_MemberMatch]]:
        by_span: dict[tuple[int, int], list[_MemberMatch]] = {}
        for match in matches:
            by_span.setdefault((match.start, match.end), []).append(match)

        ranked_spans: list[tuple[int, int, int, list[_MemberMatch]]] = []
        for (start, end), span_matches in by_span.items():
            best_rank = min(cls._source_rank(match.candidate.source) for match in span_matches)
            preferred = [
                match
                for match in span_matches
                if cls._source_rank(match.candidate.source) == best_rank
            ]
            ranked_spans.append((start, end, best_rank, preferred))
        ranked_spans.sort(key=lambda item: (-(item[1] - item[0]), item[2], item[0]))

        selected: list[tuple[int, int, list[_MemberMatch]]] = []
        for start, end, _, span_matches in ranked_spans:
            if any(
                start < selected_end and end > selected_start
                for selected_start, selected_end, _ in selected
            ):
                continue
            selected.append((start, end, span_matches))
        selected.sort(key=lambda item: item[0])
        return [item[2] for item in selected]

    @staticmethod
    def _mention_spans(question: str, candidate: str) -> list[tuple[int, int]]:
        folded = candidate.casefold()
        if any(character.isascii() and character.isalnum() for character in folded):
            pattern = rf"(?<!\w){re.escape(folded)}(?!\w)"
        else:
            pattern = re.escape(folded)
        return [(match.start(), match.end()) for match in re.finditer(pattern, question)]

    @staticmethod
    def _contains_mention(question: str, candidate: str) -> bool:
        folded = candidate.casefold()
        if any(character.isascii() and character.isalnum() for character in folded):
            pattern = rf"(?<!\w){re.escape(folded)}(?!\w)"
            return re.search(pattern, question) is not None
        return folded in question

    @staticmethod
    def _is_negated(question: str, mention_start: int, mention_end: int) -> bool:
        prefix = question[max(0, mention_start - 32) : mention_start]
        suffix = question[mention_end : mention_end + 8]
        return (
            re.search(
                (
                    r"(?:do\s+not\s+(?:include|use|show)|"
                    r"does\s+not\s+include|don't\s+include|"
                    r"must\s+not\s+include|should\s+not\s+include|"
                    r"exclude|excluding|except(?:\s+for)?|"
                    r"other\s+than|not|without)(?:\s+the)?\s*$"
                ),
                prefix,
            )
            is not None
            or re.search(
                r"(?:不要包括|不要包含|不要使用|不包括|不包含|排除|不要|除了|除外|非)\s*$",
                prefix,
            )
            is not None
            or re.match(r"\s*(?:以外|之外)", suffix) is not None
        )

    def _normalize_time(
        self,
        question: str,
        evaluation_clock: datetime,
        timezone_name: str,
    ) -> tuple[list[TimeWindow], list[Diagnostic]]:
        try:
            timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError, OSError, OverflowError):
            return [], [
                Diagnostic(
                    code="INVALID_TIMEZONE",
                    severity=DiagnosticSeverity.ERROR,
                    stage=DiagnosticStage.INITIALIZE,
                    message="The evaluation timezone is not a known IANA timezone.",
                    path="evaluation_timezone",
                )
            ]
        try:
            local_clock = evaluation_clock.astimezone(timezone)
        except (ValueError, OSError, OverflowError):
            return [], [
                Diagnostic(
                    code="INVALID_EVALUATION_CLOCK",
                    severity=DiagnosticSeverity.ERROR,
                    stage=DiagnosticStage.INITIALIZE,
                    message="The evaluation clock cannot be represented in the requested timezone.",
                    path="evaluation_clock",
                )
            ]
        recognizers = (
            ("上季度", self._previous_quarter),
            ("今年", self._this_year),
            ("去年", self._previous_year),
            ("本月", self._this_month),
            ("上月", self._previous_month),
        )
        windows: list[TimeWindow] = []
        diagnostics: list[Diagnostic] = []
        for phrase, resolver in recognizers:
            if phrase not in question:
                continue
            mention_start = question.index(phrase)
            if self._is_negated(question.casefold(), mention_start, mention_start + len(phrase)):
                diagnostics.append(
                    Diagnostic(
                        code="NEGATED_TIME_UNSUPPORTED",
                        severity=DiagnosticSeverity.ERROR,
                        stage=DiagnosticStage.INITIALIZE,
                        message="Negative relative-time constraints are not supported.",
                        path="question",
                        details={"mention": phrase},
                    )
                )
                continue
            try:
                start, end, grain = resolver(local_clock)
                windows.append(
                    TimeWindow(
                        source_text=phrase,
                        start=start,
                        end_exclusive=end,
                        timezone=timezone_name,
                        grain=grain,
                    )
                )
            except (ValueError, OSError, OverflowError):
                diagnostics.append(
                    Diagnostic(
                        code="INVALID_EVALUATION_CLOCK",
                        severity=DiagnosticSeverity.ERROR,
                        stage=DiagnosticStage.INITIALIZE,
                        message=(
                            "The requested time window is outside the supported datetime range."
                        ),
                        path="evaluation_clock",
                    )
                )
        return windows, diagnostics

    @staticmethod
    def _at_midnight(clock: datetime, year: int, month: int, day: int = 1) -> datetime:
        return datetime(year, month, day, tzinfo=clock.tzinfo)

    def _this_year(self, clock: datetime) -> tuple[datetime, datetime, TimeGrain]:
        return (
            self._at_midnight(clock, clock.year, 1),
            self._at_midnight(clock, clock.year + 1, 1),
            "year",
        )

    def _previous_year(self, clock: datetime) -> tuple[datetime, datetime, TimeGrain]:
        return (
            self._at_midnight(clock, clock.year - 1, 1),
            self._at_midnight(clock, clock.year, 1),
            "year",
        )

    def _previous_quarter(self, clock: datetime) -> tuple[datetime, datetime, TimeGrain]:
        current_start_month = ((clock.month - 1) // 3) * 3 + 1
        if current_start_month == 1:
            start_year, start_month = clock.year - 1, 10
        else:
            start_year, start_month = clock.year, current_start_month - 3
        return (
            self._at_midnight(clock, start_year, start_month),
            self._at_midnight(clock, clock.year, current_start_month),
            "quarter",
        )

    def _this_month(self, clock: datetime) -> tuple[datetime, datetime, TimeGrain]:
        start = self._at_midnight(clock, clock.year, clock.month)
        if clock.month == 12:
            end = self._at_midnight(clock, clock.year + 1, 1)
        else:
            end = self._at_midnight(clock, clock.year, clock.month + 1)
        return start, end, "month"

    def _previous_month(self, clock: datetime) -> tuple[datetime, datetime, TimeGrain]:
        if clock.month == 1:
            year, month = clock.year - 1, 12
        else:
            year, month = clock.year, clock.month - 1
        start = self._at_midnight(clock, year, month)
        end = self._at_midnight(clock, clock.year, clock.month)
        return start, end, "month"
