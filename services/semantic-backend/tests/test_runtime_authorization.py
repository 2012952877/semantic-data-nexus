from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from conftest import request_for
from query_runtime.coordinator import QueryCoordinator
from test_authorization import TestAuthority, context_for
from test_provider_configuration import deadline_server

from semantic_backend.auth_context import AccessDenied, request_context
from semantic_backend.models import RunState
from semantic_backend.service import OrchestrationService


class RuntimeTestAuthority(TestAuthority):
    """Unit-test signal/commit double; real PG linearization is tested separately."""

    def __init__(self):
        super().__init__()
        self.guard_lock = asyncio.Lock()
        self.commits = 0
        self.fail_on_commit = None

    @asynccontextmanager
    async def guard(self, context, pin=None, permission="run.reader", *, deadline=None):
        async with self.guard_lock:
            await self.reauthorize(context, permission)
            yield None
            self.commits += 1
            if self.fail_on_commit == self.commits:
                raise AccessDenied("Synthetic commit-time revocation")
            await self.reauthorize(context, permission)


def setup_service(monkeypatch):
    monkeypatch.setenv("SEMANTIC_NEXUS_AUTH_MODE", "service")
    authority = RuntimeTestAuthority()
    service = OrchestrationService(authority=authority)
    context = context_for()
    request = request_for("run_" + "d" * 32).model_copy(
        update={"requested_by": context.principal.principal_id}
    )
    return service, authority, context, request


async def record_for(service, request, context):
    return await service.repository.get(request.run_id, context=context)


async def test_inherited_context_does_not_authorize_public_service_calls(monkeypatch):
    service, _, context, request = setup_service(monkeypatch)
    token = request_context.set(context)
    try:
        with pytest.raises(AccessDenied, match="explicitly verified"):
            await service.start(request)
        with pytest.raises(AccessDenied, match="explicitly verified"):
            await service.get_detail(request.run_id)
    finally:
        request_context.reset(token)
        await service.shutdown()


@pytest.mark.parametrize("queued", [False, True])
async def test_revocation_after_create_cancels_before_or_while_queued(monkeypatch, queued):
    service, authority, context, request = setup_service(monkeypatch)
    if queued:
        service._semaphore = asyncio.Semaphore(0)
    try:
        await service.start(request, context=context)
        record = await record_for(service, request, context)
        authority.active = False
        await asyncio.wait_for(asyncio.shield(record.task), 2)
        assert record.status.state is RunState.FAILED
        assert record.status.diagnostics[-1].code == "AUTHORIZATION_DENIED"
        assert record.compile_response is None
        assert record.detail.manifest is None
        with pytest.raises(AccessDenied):
            await service.get_status(request.run_id, context=context)
    finally:
        await service.shutdown()


async def test_revocation_closes_actual_inflight_model_socket(monkeypatch):
    entered = asyncio.Event()
    async with deadline_server(monkeypatch, "compile", entered=entered) as (calls, disconnected):
        service, authority, context, request = setup_service(monkeypatch)
        try:
            await service.start(request, context=context)
            record = await record_for(service, request, context)
            await asyncio.wait_for(entered.wait(), 2)
            assert len(calls) == 1
            authority.active = False
            await asyncio.wait_for(asyncio.shield(record.task), 2)
            await asyncio.wait_for(disconnected.wait(), 2)
            assert record.status.state is RunState.FAILED
            assert record.status.diagnostics[-1].code == "AUTHORIZATION_DENIED"
            assert record.detail.result is None and record.detail.manifest is None
        finally:
            await service.shutdown()


async def test_execution_revocation_cancels_work_and_awaits_cleanup(monkeypatch):
    entered, cancelled, cleaned = (asyncio.Event() for _ in range(3))

    async def execution(self, plan, *, run_id=None):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(QueryCoordinator, "run", execution)
    service, authority, context, request = setup_service(monkeypatch)
    cleanup = service._resolver_cleanup_failure

    async def observed_cleanup(record):
        result = await cleanup(record)
        cleaned.set()
        return result

    monkeypatch.setattr(service, "_resolver_cleanup_failure", observed_cleanup)
    try:
        await service.start(request, context=context)
        record = await record_for(service, request, context)
        await asyncio.wait_for(entered.wait(), 2)
        authority.active = False
        await asyncio.wait_for(asyncio.shield(record.task), 2)
        assert cancelled.is_set() and cleaned.is_set()
        assert record.status.state is RunState.FAILED
        assert record.detail.manifest is None
        assert service._semaphore._value == 4
    finally:
        await service.shutdown()


@pytest.mark.parametrize("commit_number", [1, 2])
async def test_commit_exit_failure_never_publishes_result_or_success(monkeypatch, commit_number):
    service, authority, context, request = setup_service(monkeypatch)
    authority.fail_on_commit = commit_number
    try:
        await service.start(request, context=context)
        record = await record_for(service, request, context)
        await asyncio.wait_for(asyncio.shield(record.task), 3)
        assert record.status.state is RunState.FAILED
        assert record.detail.result is None and record.detail.manifest is None
        assert record.pending_detail is None
    finally:
        await service.shutdown()


async def test_shutdown_cancels_all_scopes_without_user_authorization_bypass(monkeypatch):
    service, authority, context, request = setup_service(monkeypatch)
    service._semaphore = asyncio.Semaphore(0)
    other = context_for("workspace-b")
    second = request.model_copy(
        update={
            "run_id": "run_" + "e" * 32,
            "requested_by": other.principal.principal_id,
        }
    )
    await service.start(request, context=context)
    await service.start(second, context=other)
    records = await service.repository._shutdown_records()
    authority.active = False
    await service.shutdown()
    assert len(records) == 2
    assert all(record.status.state.terminal for record in records)
    assert all(record.task.done() for record in records)
