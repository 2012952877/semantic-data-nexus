from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class DiagnosticLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    level: DiagnosticLevel
    message: str
    duration_ms: int | None = None
    request_id: str | None = None
    statement_id: str | None = None
    state: str | None = None


class DiagnosticSink(Protocol):
    def emit(self, diagnostic: Diagnostic) -> None: ...


@dataclass
class InMemoryDiagnosticSink:
    events: list[Diagnostic] = field(default_factory=list)

    def emit(self, diagnostic: Diagnostic) -> None:
        self.events.append(diagnostic)
