"""Finite state tracking and replaceable event persistence/subscription."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Protocol

from query_runtime.domain import (
    STATE_TRANSITIONS,
    TERMINAL_STATES,
    DiagnosticEvent,
    ExecutionState,
    utc_now,
)
from query_runtime.errors import RuntimeFailure


class EventStore(Protocol):
    async def claim(self, run_id: str) -> bool: ...

    async def append(self, event: DiagnosticEvent) -> None: ...

    async def list(self, run_id: str) -> tuple[DiagnosticEvent, ...]: ...

    def subscribe(
        self, run_id: str, heartbeat_seconds: float = 15.0
    ) -> AsyncIterator[DiagnosticEvent]: ...


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: dict[str, list[DiagnosticEvent]] = defaultdict(list)
        self._subscribers: dict[
            str, set[asyncio.Queue[DiagnosticEvent]]
        ] = defaultdict(set)
        self._closing_subscribers: set[asyncio.Queue[DiagnosticEvent]] = set()
        self._claimed_run_ids: set[str] = set()
        self._lock = asyncio.Lock()

    async def claim(self, run_id: str) -> bool:
        async with self._lock:
            if run_id in self._claimed_run_ids or self._events.get(run_id):
                return False
            self._claimed_run_ids.add(run_id)
            return True

    async def append(self, event: DiagnosticEvent) -> None:
        async with self._lock:
            self._events[event.run_id].append(event)
            subscribers = tuple(self._subscribers[event.run_id])
        closing: list[asyncio.Queue[DiagnosticEvent]] = []
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                while not queue.empty():
                    queue.get_nowait()
                closing.append(queue)
                continue
            if event.scope == "run" and event.state in TERMINAL_STATES:
                closing.append(queue)
        if closing:
            async with self._lock:
                for queue in closing:
                    self._subscribers[event.run_id].discard(queue)
                    self._closing_subscribers.add(queue)

    async def list(self, run_id: str) -> tuple[DiagnosticEvent, ...]:
        async with self._lock:
            return tuple(self._events.get(run_id, ()))

    async def subscribe(
        self, run_id: str, heartbeat_seconds: float = 15.0
    ) -> AsyncIterator[DiagnosticEvent]:
        queue: asyncio.Queue[DiagnosticEvent] = asyncio.Queue(maxsize=256)
        async with self._lock:
            if any(
                event.scope == "run" and event.state in TERMINAL_STATES
                for event in self._events.get(run_id, ())
            ):
                return
            self._subscribers[run_id].add(queue)
        try:
            while True:
                async with self._lock:
                    closing = queue in self._closing_subscribers
                    active = queue in self._subscribers[run_id]
                    if closing and queue.empty():
                        self._closing_subscribers.discard(queue)
                        return
                    if not closing and not active:
                        return
                if closing:
                    yield queue.get_nowait()
                    continue
                try:
                    yield await asyncio.wait_for(
                        queue.get(), timeout=heartbeat_seconds
                    )
                except TimeoutError:
                    async with self._lock:
                        closing = queue in self._closing_subscribers
                        if closing and queue.empty():
                            self._closing_subscribers.discard(queue)
                            return
                        if closing:
                            continue
                        if queue not in self._subscribers[run_id]:
                            return
                        sequence = len(self._events.get(run_id, ()))
                    yield DiagnosticEvent(
                        sequence=sequence,
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
                self._closing_subscribers.discard(queue)


class StateMachine:
    def __init__(self, initial: ExecutionState = ExecutionState.PENDING) -> None:
        self.state = initial

    def validate_transition(self, target: ExecutionState) -> None:
        if target not in STATE_TRANSITIONS[self.state]:
            raise RuntimeFailure(
                "STATE_TRANSITION_INVALID",
                f"Cannot transition from {self.state} to {target}",
                details={"from": self.state.value, "to": target.value},
            )

    def transition(self, target: ExecutionState) -> None:
        self.validate_transition(target)
        self.state = target
