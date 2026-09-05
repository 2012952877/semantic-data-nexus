from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from test_catalog_v1 import CASES, InjectedProvider, deadline, setup

from semantic_api.catalog_v1.clarification import ClarificationRecord, PostgresClarifications
from semantic_api.catalog_v1.guard import PublishedCatalogGuardAdapter
from semantic_api.catalog_v1.guarded_compiler import GuardedCatalogCompiler
from semantic_api.catalog_v1.guarded_store import GuardedClarifications
from semantic_api.catalog_v1.models import CompilerFailure

ACTIVE_CONNECTION = ContextVar("synthetic_guard_connection", default=None)
GATE = 731320032


@dataclass(frozen=True)
class Access:
    entity_ids: frozenset[str]
    field_ids: frozenset[str]
    metric_ids: frozenset[str]
    relation_ids: frozenset[str]
    member_ids: frozenset[str]


@dataclass(frozen=True)
class Decision:
    connection: object
    access: Access


class NoLegacyIO:
    async def require(self, *args):
        raise AssertionError("Guarded context must not call a second authority")

    async def create(self, *args):
        raise AssertionError("Guarded requests must not open a legacy store connection")

    def lock(self, *args):
        raise AssertionError("Guarded requests must not use legacy transactions")


class TestGuard:
    """Transaction/lock protocol fixture, NOT a mock OIDC authenticator."""

    __test__ = False

    def __init__(self, dsn, schema):
        self.dsn, self.schema = dsn, schema
        self.entries = 0
        self.pause_exit_on = None
        self.fail_exit_on = None
        self.exit_entered = asyncio.Event()
        self.exit_release = asyncio.Event()
        self.revoker_entered = asyncio.Event()
        self.revoker_pid = None
        self.connection_ids = []
        self.guard_entered = asyncio.Event()

    @asynccontextmanager
    async def connection(self):
        async with await psycopg.AsyncConnection.connect(self.dsn, autocommit=True) as connection:
            async with connection.transaction():
                await connection.execute(
                    sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(self.schema))
                )
                yield connection

    @asynccontextmanager
    async def guard(self, context, pin=None, permission="run.reader", *, deadline=None):
        assert permission == "compiler:query"
        loop = asyncio.get_running_loop()
        expires = (context.authentication.expires_at - datetime.now(UTC)).total_seconds()
        limit = min(deadline, loop.time() + max(0, expires), loop.time() + 5)
        async with asyncio.timeout_at(limit):
            async with self.connection() as connection:
                await connection.execute("SELECT pg_advisory_xact_lock_shared(%s)", (GATE,))
                assert context.scope == pin.scope
                cursor = await connection.execute(
                    "SELECT allowed,access FROM fixture_authority WHERE id=1"
                )
                allowed, access = await cursor.fetchone()
                if not allowed:
                    raise CompilerFailure("MEMBERSHIP_REVOKED")
                self.entries += 1
                entry = self.entries
                self.connection_ids.append(connection.info.backend_pid)
                self.guard_entered.set()
                token = ACTIVE_CONNECTION.set(connection)
                try:
                    yield Decision(
                        connection,
                        Access(**{k: frozenset(v) for k, v in json.loads(access).items()}),
                    )
                    if self.pause_exit_on == entry:
                        self.exit_entered.set()
                        await self.exit_release.wait()
                    if self.fail_exit_on == entry:
                        raise CompilerFailure("GUARD_COMMIT_REJECTED")
                    if datetime.now(UTC) >= context.authentication.expires_at:
                        raise CompilerFailure("AUTHORIZATION_EXPIRED")
                finally:
                    ACTIVE_CONNECTION.reset(token)

    async def change(self, *, allowed=None, access=None):
        async with self.connection() as connection:
            self.revoker_pid = connection.info.backend_pid
            self.revoker_entered.set()
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (GATE,))
            if allowed is not None:
                await connection.execute("UPDATE fixture_authority SET allowed=%s", (allowed,))
            if access is not None:
                await connection.execute(
                    "UPDATE fixture_authority SET access=%s", (json.dumps(access),)
                )

    async def inspect(self, request_id):
        async with self.connection() as connection:
            row = await (
                await connection.execute(
                    """SELECT payload,guarded_version,generation FROM compiler_clarifications_v1
                   WHERE request_id=%s""",
                    (request_id,),
                )
            ).fetchone()
            attempts = await (
                await connection.execute(
                    """SELECT row_to_json(a)::text FROM compiler_clarification_attempts_v1 a
                   JOIN compiler_clarifications_v1 r ON r.id=a.record_id
                   WHERE r.request_id=%s ORDER BY step""",
                    (request_id,),
                )
            ).fetchall()
            return row, [json.loads(a[0]) for a in attempts]


class CheckedCatalog:
    def __init__(self, source):
        self.source = PublishedCatalogGuardAdapter(source)

    async def get(self, connection, pin):
        assert ACTIVE_CONNECTION.get() is connection
        return await self.source.get(connection, pin)


class CheckedProvider(InjectedProvider):
    async def invoke(self, context, **kwargs):
        assert ACTIVE_CONNECTION.get() is None, "Model invoked inside the guard"
        return await super().invoke(context, **kwargs)


@pytest_asyncio.fixture
async def guarded_case():
    dsn = os.environ.get("TEST_COMPILER_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL required for guarded adapter tests")
    schema = "compiler_guard_" + uuid4().hex
    guard = TestGuard(dsn, schema)
    core, request, context, authority = setup(
        provider=CheckedProvider(CASES[1]["candidate"], CASES[1]["candidate"])
    )
    catalogs = CheckedCatalog(core.catalogs)
    core.authorization = NoLegacyIO()
    core.clarifications = NoLegacyIO()
    store = GuardedClarifications()
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        async with guard.connection() as connection:
            await connection.execute(
                "CREATE TABLE fixture_authority(id integer PRIMARY KEY,allowed boolean,access text)"
            )
            access = {k: sorted(v) for k, v in authority.access.model_dump().items()}
            await connection.execute(
                "INSERT INTO fixture_authority VALUES (1,true,%s)", (json.dumps(access),)
            )
            await store.initialize(connection)
        compiler = GuardedCatalogCompiler(core=core, guard=guard, catalogs=catalogs, store=store)
        yield SimpleNamespace(
            compiler=compiler,
            core=core,
            request=request,
            context=context,
            guard=guard,
            store=store,
            catalogs=catalogs,
            access=access,
        )
    finally:
        guard.exit_release.set()
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


async def compile_case(case, request=None):
    return await case.compiler.compile(
        request or case.request, context=case.context, deadline=deadline()
    )


async def wait_for(event):
    await asyncio.wait_for(event.wait(), 5)


async def test_guarded_direct_reservation_model_outside_and_idempotent_replay(guarded_case):
    case = guarded_case
    result = await compile_case(case)
    assert result.status == "compiled"
    assert len(case.core.provider.calls) == 1
    assert await compile_case(case) == result
    assert len(case.core.provider.calls) == 1
    row, attempts = await case.guard.inspect(case.request.request_id)
    assert row[1] == 1 and len(attempts) == 1
    attempt = attempts[0]
    assert attempt["state"] == "completed"
    assert attempt["reserved_calls"] == 2
    assert attempt["reserved_input_tokens"] == case.core.limits.max_total_input_tokens
    assert attempt["used_input_tokens"] is None
    assert datetime.fromisoformat(attempt["deadline_at"]).tzinfo is not None
    assert attempt["deadline_at"] == attempt["lease_until"]
    assert len(set(case.guard.connection_ids)) == len(case.guard.connection_ids)


async def test_guarded_two_client_duplicate_is_in_progress_without_second_model(guarded_case):
    case = guarded_case
    case.core.provider.pause = asyncio.Event()
    task = asyncio.create_task(compile_case(case))
    await wait_for(case.core.provider.entered)
    reserved_row, reserved_attempts = await case.guard.inspect(case.request.request_id)
    assert json.loads(reserved_row[0])["current"] is None
    assert reserved_attempts[0]["state"] == "in_flight"
    assert reserved_attempts[0]["reserved_calls"] == 2
    second = GuardedCatalogCompiler(
        core=case.core, guard=case.guard, catalogs=case.catalogs, store=GuardedClarifications()
    )
    try:
        with pytest.raises(CompilerFailure, match="COMPILATION_IN_PROGRESS"):
            await second.compile(case.request, context=case.context, deadline=deadline())
        with pytest.raises(CompilerFailure, match="IDEMPOTENCY_CONFLICT"):
            await compile_case(case, case.request.model_copy(update={"question": "different"}))
        assert len(case.core.provider.calls) == 1
        case.core.provider.pause.set()
        result = await task
        assert (
            await second.compile(case.request, context=case.context, deadline=deadline()) == result
        )
        assert len(case.core.provider.calls) == 1
    finally:
        case.core.provider.pause.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("change", ["membership", "grants"])
async def test_guarded_revoke_during_model_does_not_block_or_publish(guarded_case, change):
    case = guarded_case
    case.core.provider.pause = asyncio.Event()
    task = asyncio.create_task(compile_case(case))
    await wait_for(case.core.provider.entered)
    try:
        if change == "membership":
            await asyncio.wait_for(case.guard.change(allowed=False), 1)
            error = "MEMBERSHIP_REVOKED"
        else:
            access = {**case.access, "member_ids": []}
            await asyncio.wait_for(case.guard.change(access=access), 1)
            error = "CLARIFICATION_CONTEXT_CHANGED"
        case.core.provider.pause.set()
        with pytest.raises(CompilerFailure, match=error):
            await task
        row, attempts = await case.guard.inspect(case.request.request_id)
        assert json.loads(row[0])["current"] is None
        assert attempts[0]["state"] == "in_flight"
        with pytest.raises(CompilerFailure, match=error):
            await compile_case(case)
        assert len(case.core.provider.calls) == 1
    finally:
        case.core.provider.pause.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_guarded_commit_blocks_revoker_and_publishes_only_after_exit(guarded_case):
    case = guarded_case
    case.guard.pause_exit_on = 2
    task = asyncio.create_task(compile_case(case))
    await wait_for(case.guard.exit_entered)
    revoker = asyncio.create_task(case.guard.change(allowed=False))
    await wait_for(case.guard.revoker_entered)
    try:
        async with asyncio.timeout(2):
            async with case.guard.connection() as connection:
                while True:
                    row = await (
                        await connection.execute(
                            "SELECT pg_blocking_pids(%s)", (case.guard.revoker_pid,)
                        )
                    ).fetchone()
                    if row[0]:
                        assert case.guard.connection_ids[-1] in row[0]
                        break
        assert not task.done() and not revoker.done()
        case.guard.exit_release.set()
        result = await task
        await revoker
        assert result.status == "compiled"
        with pytest.raises(CompilerFailure, match="MEMBERSHIP_REVOKED"):
            await compile_case(case)
    finally:
        case.guard.exit_release.set()
        await asyncio.gather(task, revoker, return_exceptions=True)


@pytest.mark.parametrize("failed_exit", [1, 2])
async def test_guarded_exit_failure_never_dispatches_or_replays_uncommitted_response(
    guarded_case, failed_exit
):
    case = guarded_case
    case.guard.fail_exit_on = failed_exit
    with pytest.raises(CompilerFailure, match="GUARD_COMMIT_REJECTED"):
        await compile_case(case)
    row, attempts = await case.guard.inspect(case.request.request_id)
    if failed_exit == 1:
        assert row is None and not attempts and not case.core.provider.calls
        assert (await compile_case(case)).status == "compiled"
    else:
        assert json.loads(row[0])["current"] is None
        assert attempts[0]["state"] == "in_flight"
        with pytest.raises(CompilerFailure, match="COMPILATION_IN_PROGRESS"):
            await compile_case(case)
        assert len(case.core.provider.calls) == 1


async def test_guarded_cancel_and_expired_lease_preserve_fence_without_takeover(guarded_case):
    case = guarded_case
    case.core.provider.pause = asyncio.Event()
    task = asyncio.create_task(compile_case(case))
    await wait_for(case.core.provider.entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _, before = await case.guard.inspect(case.request.request_id)
    with pytest.raises(CompilerFailure, match="COMPILATION_IN_PROGRESS"):
        await compile_case(case)
    async with case.guard.connection() as connection:
        await connection.execute(
            """UPDATE compiler_clarification_attempts_v1
               SET lease_until=clock_timestamp()-interval '1 second'
               WHERE fence=%s""",
            (before[0]["fence"],),
        )
    for _ in range(2):
        with pytest.raises(CompilerFailure, match="COMPILATION_OUTCOME_UNKNOWN"):
            await compile_case(case)
    _, after = await case.guard.inspect(case.request.request_id)
    assert after[0]["state"] == "outcome_unknown"
    assert before[0]["fence"] == after[0]["fence"]
    assert before[0]["reserved_calls"] == after[0]["reserved_calls"]
    assert len(case.core.provider.calls) == 1


async def test_guarded_clarification_step_conflict_old_step_and_current_grants(guarded_case):
    case = guarded_case
    request = case.request.model_copy(update={"question": "yield for alpha above score 1"})
    initial = await compile_case(case, request)
    assert initial.status == "clarification" and not case.core.provider.calls
    args = dict(revision=1, context=case.context, pin=request.catalog, deadline=deadline())
    result = await case.compiler.resume(initial.clarification_id, "lab.mean_yield", **args)
    assert result.status == "compiled"
    assert await case.compiler.resume(initial.clarification_id, "lab.mean_yield", **args) == result
    with pytest.raises(CompilerFailure, match="IDEMPOTENCY_CONFLICT"):
        await case.compiler.resume(initial.clarification_id, "lab.total_yield", **args)
    with pytest.raises(CompilerFailure, match="CLARIFICATION_REVISION"):
        await case.compiler.resume(
            initial.clarification_id, "lab.mean_yield", **{**args, "revision": 2}
        )
    await case.guard.change(access={**case.access, "metric_ids": ["lab.mean_yield"]})
    with pytest.raises(CompilerFailure, match="CLARIFICATION_CONTEXT_CHANGED"):
        await case.compiler.resume(initial.clarification_id, "lab.mean_yield", **args)
    assert len(case.core.provider.calls) == 1


@pytest.mark.parametrize("pending", [False, True])
async def test_guarded_upgrade_preserves_legacy_rows_and_unknown_pending(guarded_case, pending):
    case = guarded_case
    document = await case.catalogs.source.catalogs.get(case.request.catalog)
    from semantic_api.catalog_v1.trust import CatalogAccess

    owner, initialized, identity = case.core._context_from_catalog(
        case.request, case.context, document, CatalogAccess.model_validate(case.access)
    )
    response = None if pending else await case.core._generate(initialized)
    async with case.guard.connection() as connection:
        record = ClarificationRecord(
            id="clarification-legacy",
            owner=owner,
            request=case.request,
            context=initialized,
            authority_fingerprint=identity,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            current=response,
        )
        from semantic_api.catalog_v1.catalog import fingerprint

        payload = record.model_dump_json()
        await connection.execute(
            """INSERT INTO compiler_clarifications_v1
               (id,owner_hash,request_id,request_hash,expires_at,payload)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (
                record.id,
                fingerprint(owner.model_dump(mode="json")),
                record.request.request_id,
                fingerprint(record.request.model_dump(mode="json")),
                record.expires_at,
                payload,
            ),
        )
        await case.store.initialize(connection)
    calls = len(case.core.provider.calls)
    if pending:
        with pytest.raises(CompilerFailure, match="COMPILATION_OUTCOME_UNKNOWN"):
            await compile_case(case)
    else:
        assert await compile_case(case) == response
    row, attempts = await case.guard.inspect(case.request.request_id)
    assert row[0] == payload and row[1] == 1
    assert len(case.core.provider.calls) == calls
    if pending:
        assert attempts[0]["origin"] == "legacy_unknown"
        assert attempts[0]["reserved_input_tokens"] is None

    class ExistingConnectionStore(PostgresClarifications):
        @asynccontextmanager
        async def _connection(self):
            async with case.guard.connection() as connection:
                yield connection

    with pytest.raises(CompilerFailure, match="GUARDED_COMPILER_REQUIRED"):
        async with ExistingConnectionStore("").lock(record.id, owner):
            pytest.fail("Legacy store admitted a guarded row")


async def test_guarded_real_socket_candidate_and_usage(guarded_case):
    from test_structured_provider import SECRET, completion, mock_server, reply, settings

    from semantic_api.catalog_v1.provider import CatalogHTTPProvider

    case = guarded_case
    async with mock_server(reply(completion(CASES[1]["candidate"]))) as mock:
        provider = CatalogHTTPProvider(settings(mock.endpoint), SECRET)
        case.core.provider = provider
        result = await compile_case(case)
        assert result.status == "compiled"
        assert result.graph.model_dump(mode="json") == CASES[1]["candidate"]["graph"]
        assert result.input_tokens == 150 and result.output_tokens == 250
        _, attempts = await case.guard.inspect(case.request.request_id)
        assert attempts[0]["used_input_tokens"] == 150
        assert len(mock.requests) == 1
        await provider.aclose()


@pytest.mark.parametrize("tamper", ["fence", "generation"])
async def test_guarded_stale_attempt_cannot_commit(guarded_case, tamper):
    case = guarded_case
    case.core.provider.pause = asyncio.Event()
    task = asyncio.create_task(compile_case(case))
    await wait_for(case.core.provider.entered)
    try:
        _, attempts = await case.guard.inspect(case.request.request_id)
        async with case.guard.connection() as connection:
            if tamper == "fence":
                await connection.execute(
                    "UPDATE compiler_clarification_attempts_v1 SET fence=%s WHERE record_id=%s",
                    (uuid4().hex, attempts[0]["record_id"]),
                )
            else:
                await connection.execute(
                    "UPDATE compiler_clarifications_v1 SET generation=generation+1 WHERE id=%s",
                    (attempts[0]["record_id"],),
                )
        case.core.provider.pause.set()
        with pytest.raises(CompilerFailure, match="ATTEMPT_FENCED"):
            await task
        after, _ = await case.guard.inspect(case.request.request_id)
        assert json.loads(after[0])["current"] is None
        assert len(case.core.provider.calls) == 1
    finally:
        case.core.provider.pause.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("interrupt", ["cancel", "expiry"])
async def test_guarded_row_wait_rollback_never_dispatches(guarded_case, interrupt):
    case = guarded_case
    request = case.request.model_copy(update={"question": "yield for alpha above score 1"})
    initial = await compile_case(case, request)
    before, attempts = await case.guard.inspect(request.request_id)
    if interrupt == "expiry":
        case.context.authentication.expires_at = datetime.now(UTC) + timedelta(seconds=0.3)
    async with case.guard.connection() as holder:
        await holder.execute(
            "SELECT id FROM compiler_clarifications_v1 WHERE id=%s FOR UPDATE",
            (initial.clarification_id,),
        )
        case.guard.guard_entered.clear()
        task = asyncio.create_task(
            case.compiler.resume(
                initial.clarification_id,
                "lab.mean_yield",
                revision=1,
                context=case.context,
                pin=request.catalog,
                deadline=deadline(),
            )
        )
        try:
            await wait_for(case.guard.guard_entered)
            waiting_pid = case.guard.connection_ids[-1]
            async with asyncio.timeout(2):
                async with case.guard.connection() as observer:
                    while True:
                        row = await (
                            await observer.execute(
                                "SELECT pg_blocking_pids(%s)",
                                (waiting_pid,),
                            )
                        ).fetchone()
                        if holder.info.backend_pid in row[0]:
                            break
            if interrupt == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(TimeoutError):
                    await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    after, after_attempts = await case.guard.inspect(request.request_id)
    assert after == before and after_attempts == attempts
    assert not case.core.provider.calls


async def test_guarded_preserves_and_continues_legacy_clarification(guarded_case):
    case = guarded_case
    request = case.request.model_copy(update={"question": "yield for alpha above score 1"})
    initial = await compile_case(case, request)
    # Simulate the reviewed legacy table shape/data, then perform an additive upgrade.
    async with case.guard.connection() as connection:
        await connection.execute("DELETE FROM compiler_clarification_attempts_v1")
        await connection.execute(
            "ALTER TABLE compiler_clarifications_v1 DROP COLUMN guarded_version"
        )
        await connection.execute("ALTER TABLE compiler_clarifications_v1 DROP COLUMN generation")
        payload_before = (
            await (
                await connection.execute(
                    "SELECT payload FROM compiler_clarifications_v1 WHERE id=%s",
                    (initial.clarification_id,),
                )
            ).fetchone()
        )[0]
        await case.store.initialize(connection)
        payload_after = (
            await (
                await connection.execute(
                    "SELECT payload FROM compiler_clarifications_v1 WHERE id=%s",
                    (initial.clarification_id,),
                )
            ).fetchone()
        )[0]
        assert payload_after == payload_before
    assert await compile_case(case, request) == initial
    result = await case.compiler.resume(
        initial.clarification_id,
        "lab.mean_yield",
        revision=1,
        context=case.context,
        pin=request.catalog,
        deadline=deadline(),
    )
    assert result.status == "compiled"


@pytest.mark.parametrize("changed", ["scope", "issuer", "subject", "membership"])
async def test_guarded_clarification_ownership_never_enumerates_other_owner(guarded_case, changed):
    import copy

    case = guarded_case
    request = case.request.model_copy(update={"question": "yield for alpha above score 1"})
    initial = await compile_case(case, request)
    context = copy.deepcopy(case.context)
    if changed == "scope":
        context.scope = context.scope.model_copy(update={"workspace_id": "other"})
        error = "RESOURCE_NOT_AVAILABLE"
    else:
        error = "CLARIFICATION_NOT_AVAILABLE"
        if changed == "issuer":
            context.principal.issuer = "https://other.invalid"
        elif changed == "subject":
            context.principal.subject = "other-subject"
        else:
            context.membership.revision += 1
    with pytest.raises(CompilerFailure, match=error):
        await case.compiler.resume(
            initial.clarification_id,
            "lab.mean_yield",
            revision=1,
            context=context,
            pin=request.catalog,
            deadline=deadline(),
        )
    assert not case.core.provider.calls


async def test_guarded_configuration_change_after_prepare_does_not_dispatch(guarded_case):
    case = guarded_case
    original = case.core.provider
    case.guard.pause_exit_on = 1
    task = asyncio.create_task(compile_case(case))
    await wait_for(case.guard.exit_entered)
    try:
        case.core.provider = CheckedProvider(CASES[1]["candidate"])
        case.guard.exit_release.set()
        with pytest.raises(CompilerFailure, match="COMPILER_CONFIGURATION_CHANGED"):
            await task
        assert not original.calls and not case.core.provider.calls
    finally:
        case.guard.exit_release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_guarded_record_expiry_during_commit_exit_rolls_back(guarded_case):
    from semantic_api.catalog_v1.compiler import CompilerLimits

    case = guarded_case
    case.core.limits = CompilerLimits(clarification_ttl_seconds=1)
    case.guard.pause_exit_on = 2
    task = asyncio.create_task(compile_case(case))
    await wait_for(case.guard.exit_entered)
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, 2)
        row, attempts = await case.guard.inspect(case.request.request_id)
        assert json.loads(row[0])["current"] is None
        assert attempts[0]["state"] == "in_flight"
    finally:
        case.guard.exit_release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "change", ["limits", "provider", "provider_config", "capabilities", "none"]
)
async def test_guarded_claim_wait_uses_one_context_budget_dispatch_snapshot(guarded_case, change):
    from semantic_api.catalog_v1.compiler import CompilerLimits
    from semantic_api.catalog_v1.trust import CatalogAccess

    case = guarded_case

    class MeteredProvider(CheckedProvider):
        async def invoke(self, context, **kwargs):
            result = await super().invoke(context, **kwargs)
            return result.model_copy(
                update={
                    "input_tokens": 1,
                    "output_tokens": 1 if change == "none" else 2,
                }
            )

    class PausingClaimStore(GuardedClarifications):
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.reservation = None

        async def claim(self, connection, stored, **kwargs):
            assert ACTIVE_CONNECTION.get() is connection
            self.reservation = kwargs["reservation"]
            self.entered.set()
            await self.release.wait()
            return await super().claim(connection, stored, **kwargs)

    original = MeteredProvider(CASES[1]["candidate"])
    case.core.provider = original
    case.core.limits = CompilerLimits(max_total_output_tokens=1)
    captured = case.core.configuration()
    document = await case.catalogs.source.catalogs.get(case.request.catalog)
    _, _, original_identity = case.core._context_from_catalog(
        case.request,
        case.context,
        document,
        CatalogAccess.model_validate(case.access),
        configuration=captured,
    )
    pausing = PausingClaimStore()
    case.compiler.store = pausing
    task = asyncio.create_task(compile_case(case))
    await wait_for(pausing.entered)
    try:
        assert pausing.reservation.output_tokens == 1
        if change == "limits":
            case.core.limits = CompilerLimits(max_total_output_tokens=10)
        elif change == "provider":
            case.core.provider = MeteredProvider(CASES[1]["candidate"])
        elif change == "provider_config":
            original.configuration_fingerprint = "synthetic-new-provider-configuration"
        elif change == "capabilities":
            case.core.capabilities = case.core.capabilities[:-1]
        pausing.release.set()
        if change == "none":
            result = await task
            assert result.status == "compiled" and result.output_tokens == 1
            assert len(original.calls) == 1
        else:
            with pytest.raises(CompilerFailure, match="COMPILER_CONFIGURATION_CHANGED"):
                await task
            assert not original.calls and not case.core.provider.calls
        row, attempts = await case.guard.inspect(case.request.request_id)
        assert json.loads(row[0])["authority_fingerprint"] == original_identity
        assert attempts[0]["reserved_output_tokens"] == 1
        assert attempts[0]["state"] == ("completed" if change == "none" else "in_flight")
        assert captured.limits.max_total_output_tokens == 1
    finally:
        pausing.release.set()
        await asyncio.gather(task, return_exceptions=True)
