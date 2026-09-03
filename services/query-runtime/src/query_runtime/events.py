"""Finite state tracking and replaceable event persistence/subscription."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Protocol

from query_runtime.domain import (
    STATE_TRANSITIONS,
    DiagnosticEvent,
    ExecutionState,
    utc_now,
)
from query_runtime.errors import RuntimeFailure


class EventStore(Protocol):
    async def append(self, event: DiagnosticEvent) -> None: ...

    async def list(self, run_id: str) -> tuple[DiagnosticEvent, ...]: ...

    def subscribe(
        self, run_id: str, heartbeat_seconds: float = 15.0
    ) -> AsyncIterator[DiagnosticEvent]: ...


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: dict[str, list[DiagnosticEvent]] = defaultdict(list)
        self._subscribers: dict[str, set[asyncio.Queue[DiagnosticEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def append(self, event: DiagnosticEvent) -> None:
        async with self._lock:
            self._events[event.run_id].append(event)
            subscribers = tuple(self._subscribers[event.run_id])
        slow: list[asyncio.Queue[DiagnosticEvent]] = []
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                slow.append(queue)
        if slow:
            async with self._lock:
                for queue in slow:
                    self._subscribers[event.run_id].discard(queue)

    async def list(self, run_id: str) -> tuple[DiagnosticEvent, ...]:
        async with self._lock:
            return tuple(self._events.get(run_id, ()))

    async def subscribe(
        self, run_id: str, heartbeat_seconds: float = 15.0
    ) -> AsyncIterator[DiagnosticEvent]:
        queue: asyncio.Queue[DiagnosticEvent] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers[run_id].add(queue)
        try:
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                except TimeoutError:
                    yield DiagnosticEvent(
                        sequence=len(await self.list(run_id)),
                        run_id=run_id,
                        scope="run",
                        scope_id=run_id,
                        state=ExecutionState.RUNNING,
                        code="HEARTBEAT",
                        message="Run event stream heartbeat",
                        timestamp=utc_now(),
                    )
        finally:
            async with self._lock:
                self._subscribers[run_id].discard(queue)


class StateMachine:
    def __init__(self, initial: ExecutionState = ExecutionState.PENDING) -> None:
        self.state = initial

    def transition(self, target: ExecutionState) -> None:
        if target not in STATE_TRANSITIONS[self.state]:
            raise RuntimeFailure(
                "STATE_TRANSITION_INVALID",
                f"Cannot transition from {self.state} to {target}",
                details={"from": self.state.value, "to": target.value},
            )
        self.state = target
