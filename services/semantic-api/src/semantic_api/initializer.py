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

        member_matches: dict[str, list[_ResolutionCandidate]] = {}
        for field in self.registry.document.fields:
            for member in field.members:
                for source_text, source in self._match_forms(
                    question_folded,
                    member.id,
                    member.label,
                    member.synonyms,
                    mode,
                ):
                    member_matches.setdefault(source_text.casefold(), []).append(
                        _ResolutionCandidate(
                            source_text,
                            ResolvedTermKind.MEMBER,
                            member.id,
                            source,
                        )
                    )

        diagnostics: list[Diagnostic] = []
        for source_key, matches in member_matches.items():
            machine_ids = sorted({match.machine_id for match in matches})
            if len(machine_ids) > 1:
                diagnostics.append(
                    Diagnostic(
                        code="AMBIGUOUS_MEMBER",
                        severity=DiagnosticSeverity.ERROR,
                        stage=DiagnosticStage.INITIALIZE,
                        message="A member mention matches multiple governed members.",
                        path="question",
                        details={
                            "mention": source_key,
                            "candidate_count": len(machine_ids),
                            "candidates": ",".join(machine_ids),
                        },
                    )
                )
                continue
            candidates.append(matches[0])

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

    @staticmethod
    def _contains_mention(question: str, candidate: str) -> bool:
        folded = candidate.casefold()
        if any(character.isascii() and character.isalnum() for character in folded):
            pattern = rf"(?<!\w){re.escape(folded)}(?!\w)"
            return re.search(pattern, question) is not None
        return folded in question

    def _normalize_time(
        self,
        question: str,
        evaluation_clock: datetime,
        timezone_name: str,
    ) -> tuple[list[TimeWindow], list[Diagnostic]]:
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            return [], [
                Diagnostic(
                    code="INVALID_TIMEZONE",
                    severity=DiagnosticSeverity.ERROR,
                    stage=DiagnosticStage.INITIALIZE,
                    message="The evaluation timezone is not a known IANA timezone.",
                    path="evaluation_timezone",
                )
            ]
        local_clock = evaluation_clock.astimezone(timezone)
        recognizers = (
            ("上季度", self._previous_quarter),
            ("今年", self._this_year),
            ("去年", self._previous_year),
            ("本月", self._this_month),
            ("上月", self._previous_month),
        )
        windows: list[TimeWindow] = []
        for phrase, resolver in recognizers:
            if phrase not in question:
                continue
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
        return windows, []

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
