"""One ASGI receive owner preserves body messages and observes catalog disconnects."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from contextvars import ContextVar
from typing import Any

from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CATALOG_ABORT: ContextVar[asyncio.Event | None] = ContextVar("catalog_abort", default=None)
DISCONNECT_KEY = "nexus.catalog_disconnect"
_LATE_OPERATIONS: set[asyncio.Task[Any]] = set()
_LOGGER = logging.getLogger(__name__)


def require_connected() -> None:
    event = CATALOG_ABORT.get()
    if event is not None and event.is_set():
        raise asyncio.CancelledError


def _observe(task: asyncio.Task[Any]) -> None:
    _LATE_OPERATIONS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        _LOGGER.warning("CATALOG_DISCONNECTED_OPERATION_FAILED")


class CatalogDisconnect:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/v1/catalog/"):
            await self.app(scope, receive, send)
            return
        disconnected = asyncio.Event()
        scope[DISCONNECT_KEY] = disconnected
        messages: asyncio.Queue[Message] = asyncio.Queue(maxsize=1)

        async def pump() -> None:
            while True:
                try:
                    message = await receive()
                except OSError:
                    message = {"type": "http.disconnect"}
                if message["type"] == "http.disconnect":
                    disconnected.set()
                await messages.put(message)
                if message["type"] == "http.disconnect":
                    return

        async def queued_receive() -> Message:
            if disconnected.is_set() and messages.empty():
                return {"type": "http.disconnect"}
            return await messages.get()

        reader = asyncio.create_task(pump())
        try:
            await self.app(scope, queued_receive, send)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            failure = None if reader.cancelled() else reader.exception()
            if failure is not None:
                raise failure


async def run_connected[T](
    operation: Coroutine[Any, Any, T], disconnected: asyncio.Event
) -> T | Response:
    token = CATALOG_ABORT.set(disconnected)
    work = asyncio.create_task(operation)
    lost = asyncio.create_task(disconnected.wait())
    try:
        done, _ = await asyncio.wait({work, lost}, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()
        work.cancel()
        await asyncio.wait({work}, timeout=0.05)
        if work.done():
            _observe(work)
        else:
            _LATE_OPERATIONS.add(work)
            work.add_done_callback(_observe)
        return Response(status_code=499)
    finally:
        CATALOG_ABORT.reset(token)
        lost.cancel()
        await asyncio.gather(lost, return_exceptions=True)
        if not work.done() and work not in _LATE_OPERATIONS:
            disconnected.set()
            work.cancel()
            _LATE_OPERATIONS.add(work)
            work.add_done_callback(_observe)
