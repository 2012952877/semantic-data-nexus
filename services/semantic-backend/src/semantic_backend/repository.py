from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from query_runtime.coordinator import QueryCoordinator
from query_runtime.domain import PhysicalPlan
from semantic_api.models import CompileResponse

from semantic_backend.auth_context import TrustedContext, get_trusted_context, legacy_development
from semantic_backend.models import (
    LineageDetail,
    RunDetail,
    RunStatus,
    SqgSummary,
    StartRunRequest,
)


class RunNotFoundError(LookupError):
    pass


class RunConflictError(ValueError):
    pass


class RunCapacityError(RuntimeError):
    pass


@dataclass
class RunRecord:
    request: StartRunRequest
    status: RunStatus
    detail: RunDetail
    trusted_context: TrustedContext | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task[None] | None = None
    coordinator: QueryCoordinator | None = None
    cancel_requested: bool = False
    cancel_accepted: bool = False
    terminal_observed: bool = False
    compile_response: CompileResponse | None = None
    physical_plan: PhysicalPlan | None = None
    deadline: float | None = None


class RunRepository(Protocol):
    async def create(
        self, request: StartRunRequest, status: RunStatus
    ) -> tuple[RunRecord, bool]: ...

    async def get(self, run_id: str) -> RunRecord: ...

    async def list_records(self) -> tuple[RunRecord, ...]: ...


class InMemoryRunRepository:
    def __init__(
        self,
        *,
        max_runs: int = 100,
        retention: timedelta = timedelta(hours=1),
    ) -> None:
        self._max_runs = max_runs
        self._retention = retention
        self._records: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        request: StartRunRequest,
        status: RunStatus,
    ) -> tuple[RunRecord, bool]:
        async with self._lock:
            context = None if legacy_development() else get_trusted_context()
            self._prune_locked()
            existing = self._records.get(request.run_id)
            if existing is not None:
                if (existing.trusted_context.scope if existing.trusted_context else None) != (
                    context.scope if context else None
                ):
                    raise RunNotFoundError(request.run_id)
                if existing.request != request:
                    raise RunConflictError("run_id is already bound to a different request")
                return existing, False
            if len(self._records) >= self._max_runs:
                raise RunCapacityError("the in-memory run capacity has been reached")
            detail = RunDetail(
                run_id=request.run_id,
                question=request.question,
                sqg=SqgSummary(
                    version="sqg.v0",
                    intent=request.question[:512],
                    ontology="pending",
                    policy_checks=["validation_pending"],
                ),
                lineage=LineageDetail(run_id=request.run_id),
            )
            record = RunRecord(
                request=request, status=status, detail=detail, trusted_context=context
            )
            self._records[request.run_id] = record
            return record, True

    async def get(self, run_id: str) -> RunRecord:
        async with self._lock:
            self._prune_locked()
            try:
                record = self._records[run_id]
            except KeyError as exc:
                raise RunNotFoundError(run_id) from exc
            context = None if legacy_development() else get_trusted_context()
            if (record.trusted_context.scope if record.trusted_context else None) != (
                context.scope if context else None
            ):
                raise RunNotFoundError(run_id)
            return record

    async def list_records(self) -> tuple[RunRecord, ...]:
        async with self._lock:
            self._prune_locked()
            context = None if legacy_development() else get_trusted_context()
            return tuple(
                record
                for record in self._records.values()
                if (record.trusted_context.scope if record.trusted_context else None)
                == (context.scope if context else None)
            )

    def _prune_locked(self) -> None:
        cutoff = datetime.now(UTC) - self._retention
        stale = [
            run_id
            for run_id, record in self._records.items()
            if record.status.finalized_at is not None and record.status.finalized_at < cutoff
        ]
        for run_id in stale:
            self._records.pop(run_id, None)
