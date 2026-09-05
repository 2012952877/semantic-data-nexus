from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from conftest import request_for
from query_runtime.coordinator import QueryCoordinator
from test_authorization import TestAuthority, context_for
from test_databricks_adapter import BlockingConnector
from test_provider_configuration import deadline_server

from semantic_backend.auth_context import AccessDenied, request_context
from semantic_backend.databricks_adapter import (
    DatabricksFragmentTranslator,
    DatabricksSourceAdapter,
)
from semantic_backend.models import RunState
from semantic_backend.service import OrchestrationService


class RuntimeTestAuthority(TestAuthority):
    """Unit-test signal/commit double; real PG linearization is tested separately."""

    def __init__(self):
        super().__init__()
        self.guard_lock = asyncio.Lock()
        self.commits = 0
        self.fail_on_commit = None
        self.revoked_principals = set()

    async def reauthorize(self, context, permission):
        if context.principal.principal_id in self.revoked_principals:
            raise AccessDenied("Synthetic account revoked")
        await super().reauthorize(context, permission)

    @asynccontextmanager
    async def guard(self, context, pin=None, permission="run.reader", *, deadline=None):
        async with self.guard_lock:
            await self.reauthorize(context, permission)
            yield None
            self.commits += 1
            if self.fail_on_commit == self.commits:
                raise AccessDenied("Synthetic commit-time revocation")
            await self.reauthorize(context, permission)


def setup_service(monkeypatch, **options):
    monkeypatch.setenv("SEMANTIC_NEXUS_AUTH_MODE", "service")
    authority = RuntimeTestAuthority()
    service = OrchestrationService(authority=authority, **options)
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


def blocking_service(monkeypatch):
    started, cleaned, cleanup_entered = (asyncio.Event() for _ in range(3))
    resolver = DatabricksSourceAdapter(
        BlockingConnector(started, asyncio.Event(), cleaned),
        DatabricksFragmentTranslator(catalog="synthetic_demo", schema="analytics"),
        timeout_seconds=0.8,
    )
    service, authority, context, request = setup_service(monkeypatch, resolver=resolver)
    original = service._resolver_cleanup_failure

    async def observe_cleanup(record):
        cleanup_entered.set()
        return await original(record)

    monkeypatch.setattr(service, "_resolver_cleanup_failure", observe_cleanup)
    return service, authority, context, request, resolver, started, cleaned, cleanup_entered


async def test_timeout_cleanup_then_revocation_drains_actual_connector_and_terminalizes(
    monkeypatch,
):
    import semantic_backend.service as service_module

    monkeypatch.setattr(service_module, "_RUN_TIMEOUT_SECONDS", 0.1)
    service, authority, context, request, resolver, started, cleaned, entered = blocking_service(
        monkeypatch
    )
    try:
        await service.start(request, context=context)
        record = await record_for(service, request, context)
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(entered.wait(), 2)
        authority.active = False
        await asyncio.wait_for(asyncio.shield(record.task), 2)
        assert record.status.state is RunState.FAILED
        assert record.status.diagnostics[-1].code == "DATABRICKS_CANCELLATION_UNCONFIRMED"
        assert record.detail.manifest is None
        assert cleaned.is_set() and not resolver._active_contexts
        assert record.revocation_task.done()
    finally:
        await asyncio.wait_for(cleaned.wait(), 3)
        await service.shutdown()


@pytest.mark.parametrize("first_cause", ["timeout", "revocation"])
@pytest.mark.parametrize(
    "second_action", ["cancel", "aborted-cancel-request", "shutdown", "aborted-shutdown"]
)
async def test_revocation_cleanup_is_not_interrupted_by_other_cancellation(
    monkeypatch, first_cause, second_action
):
    import semantic_backend.service as service_module

    if first_cause == "timeout":
        monkeypatch.setattr(service_module, "_RUN_TIMEOUT_SECONDS", 0.1)
    service, authority, context, request, resolver, started, cleaned, entered = blocking_service(
        monkeypatch
    )
    survivor = context.model_copy(
        update={
            "principal": context.principal.model_copy(update={"principal_id": "survivor"}),
            "membership": context.membership.model_copy(
                update={"membership_id": "survivor-membership"}
            ),
        }
    )
    try:
        await service.start(request, context=context)
        record = await record_for(service, request, context)
        await asyncio.wait_for(started.wait(), 2)
        if first_cause == "revocation":
            authority.revoked_principals.add(context.principal.principal_id)
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(
            service.shutdown()
            if second_action in {"shutdown", "aborted-shutdown"}
            else service.cancel(request.run_id, context=survivor)
        )
        if second_action in {"aborted-cancel-request", "aborted-shutdown"}:
            await asyncio.sleep(0.05)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
        else:
            await asyncio.wait_for(second, 2)
        await asyncio.wait_for(asyncio.shield(record.task), 2)
        assert record.status.state is RunState.FAILED
        assert record.status.diagnostics[-1].code == "DATABRICKS_CANCELLATION_UNCONFIRMED"
        assert record.detail.manifest is None and record.pending_detail is None
        assert cleaned.is_set() and not resolver._active_contexts
        assert record.revocation_task.done()
        assert record.task.exception() is None
    finally:
        await asyncio.wait_for(cleaned.wait(), 3)
        await service.shutdown()
