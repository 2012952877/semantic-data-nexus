from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from test_catalog_v1 import CASES, InjectedProvider, deadline, setup

from semantic_api.catalog_v1.clarification import PostgresClarifications
from semantic_api.catalog_v1.models import CompilerFailure


@pytest_asyncio.fixture
async def pg_store():
    dsn = os.environ.get("TEST_COMPILER_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set TEST_COMPILER_POSTGRES_DSN for disposable PostgreSQL integration tests")
    store = PostgresClarifications(dsn)
    await store.initialize()
    request_id = "synthetic-pg-" + uuid4().hex
    yield store, request_id, dsn
    async with await psycopg.AsyncConnection.connect(dsn) as connection:
        await connection.execute(
            "DELETE FROM compiler_clarifications_v1 WHERE request_id = %s", (request_id,)
        )


async def start(pg_store, *, provider=None):
    store, request_id, _ = pg_store
    compiler, request, context, authority = setup(provider=provider, store=store)
    request = request.model_copy(
        update={"request_id": request_id, "question": "yield for alpha above score 1"}
    )
    first = await compiler.compile(request, context=context, deadline=deadline())
    return compiler, request, context, authority, first


async def test_restart_two_client_idempotency_and_conflicting_answer(pg_store):
    compiler, request, context, authority, first = await start(pg_store)
    # A second repository/compiler instance reads the persisted question/choices;
    # no process-local continuation state is transferred.
    second, _, _, _ = setup(store=PostgresClarifications(pg_store[2]))
    second.authorization = authority
    kwargs = dict(context=context, pin=request.catalog, revision=1, deadline=deadline())
    results = await asyncio.gather(
        compiler.resume(first.clarification_id, "lab.mean_yield", **kwargs),
        second.resume(first.clarification_id, "lab.mean_yield", **kwargs),
    )
    assert results[0] == results[1] and results[0].status == "compiled"
    assert len(compiler.provider.calls) + len(second.provider.calls) == 1
    with pytest.raises(CompilerFailure, match="IDEMPOTENCY_CONFLICT"):
        await second.resume(first.clarification_id, "lab.total_yield", **kwargs)
    replay = await second.compile(request, context=context, deadline=deadline())
    assert replay == results[0]


async def test_transaction_rollback_on_cancellation_allows_clean_retry(pg_store):
    paused = InjectedProvider(CASES[1]["candidate"])
    paused.pause = asyncio.Event()
    compiler, request, context, _, first = await start(pg_store, provider=paused)
    kwargs = dict(context=context, pin=request.catalog, revision=1, deadline=deadline())
    task = asyncio.create_task(compiler.resume(first.clarification_id, "lab.mean_yield", **kwargs))
    await paused.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    compiler.provider = InjectedProvider(CASES[1]["candidate"])
    result = await compiler.resume(first.clarification_id, "lab.mean_yield", **kwargs)
    assert result.status == "compiled"
    assert len(compiler.provider.calls) == 1


async def test_revocation_during_resume_rolls_back_and_disallows_replay(pg_store):
    paused = InjectedProvider(CASES[1]["candidate"])
    paused.pause = asyncio.Event()
    compiler, request, context, authority, first = await start(pg_store, provider=paused)
    kwargs = dict(context=context, pin=request.catalog, revision=1, deadline=deadline())
    task = asyncio.create_task(compiler.resume(first.clarification_id, "lab.mean_yield", **kwargs))
    await paused.entered.wait()
    authority.revoked = True
    paused.pause.set()
    with pytest.raises(CompilerFailure, match="MEMBERSHIP_REVOKED"):
        await task
    async with _record(compiler, first.clarification_id, context, request.catalog) as transaction:
        assert transaction.record.history == ()
    with pytest.raises(CompilerFailure, match="MEMBERSHIP_REVOKED"):
        await compiler.resume(first.clarification_id, "lab.mean_yield", **kwargs)


async def test_access_change_while_provider_runs_does_not_commit(pg_store):
    paused = InjectedProvider(CASES[1]["candidate"])
    paused.pause = asyncio.Event()
    compiler, request, context, authority, first = await start(pg_store, provider=paused)
    task = asyncio.create_task(
        compiler.resume(
            first.clarification_id,
            "lab.mean_yield",
            context=context,
            pin=request.catalog,
            revision=1,
            deadline=deadline(),
        )
    )
    await paused.entered.wait()
    authority.access = authority.access.model_copy(
        update={"metric_ids": frozenset({"lab.mean_yield"})}
    )
    paused.pause.set()
    with pytest.raises(CompilerFailure, match="AUTHORIZATION_CHANGED"):
        await task
    async with _record(compiler, first.clarification_id, context, request.catalog) as transaction:
        assert not transaction.record.history


def _record(compiler, identifier, context, pin):
    from semantic_api.catalog_v1.trust import owner_for

    return compiler.clarifications.lock(identifier, owner_for(context, pin))


async def test_scope_pin_owner_expiry_and_request_id_conflicts(pg_store):
    compiler, request, context, _, first = await start(pg_store)
    kwargs = dict(context=context, pin=request.catalog, revision=1, deadline=deadline())
    with pytest.raises(CompilerFailure, match="IDEMPOTENCY_CONFLICT"):
        await compiler.compile(
            request.model_copy(update={"question": "yield for beta"}),
            context=context,
            deadline=deadline(),
        )
    context.principal.subject = "synthetic-other"
    with pytest.raises(CompilerFailure, match="CLARIFICATION_NOT_AVAILABLE"):
        await compiler.resume(first.clarification_id, "lab.mean_yield", **kwargs)
    context.principal.subject = "synthetic-subject"
    with pytest.raises(CompilerFailure, match="CLARIFICATION_NOT_AVAILABLE"):
        await compiler.resume(
            first.clarification_id,
            "lab.mean_yield",
            **{**kwargs, "pin": request.catalog.model_copy(update={"content_sha256": "f" * 64})},
        )
    async with await psycopg.AsyncConnection.connect(pg_store[2]) as connection:
        await connection.execute(
            "UPDATE compiler_clarifications_v1 SET expires_at = %s WHERE id = %s",
            (datetime.now(UTC) - timedelta(seconds=1), first.clarification_id),
        )
    with pytest.raises(CompilerFailure, match="CLARIFICATION_EXPIRED"):
        await compiler.resume(first.clarification_id, "lab.mean_yield", **kwargs)


async def test_lock_wait_obeys_overall_deadline(pg_store):
    compiler, request, context, _, first = await start(pg_store)
    async with _record(compiler, first.clarification_id, context, request.catalog):
        with pytest.raises(TimeoutError):
            await compiler.resume(
                first.clarification_id,
                "lab.mean_yield",
                context=context,
                pin=request.catalog,
                revision=1,
                deadline=asyncio.get_running_loop().time() + 0.05,
            )
