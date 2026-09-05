"""Explicit real-PG acceptance, run by control-api CI against its existing PostgreSQL 16 service."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo

from semantic_backend.auth_context import AccessDenied, ResourceVersion, TrustedContext
from semantic_backend.authorization import PostgresAuthorization

MIGRATIONS = (
    Path(__file__).resolve().parents[2] / "control-api/src/ControlApi/Persistence/Migrations"
)
MIGRATION_SQL = [
    (MIGRATIONS / filename).read_text(encoding="utf-8")
    for filename in ["001_control_plane.sql", "002_identity_workspace.sql"]
]


def context_for() -> TrustedContext:
    now = datetime.now(UTC)
    return TrustedContext.model_validate(
        {
            "contract_version": "trusted-context/v1",
            "scope": {"tenant_id": "tenant-a", "workspace_id": "workspace-a"},
            "principal": {
                "principal_id": "principal-a",
                "issuer": "https://identity.example.test",
                "subject": "same-sub",
            },
            "authentication": {
                "method": "oidc",
                "audience": "nexus-api",
                "authenticated_at": now - timedelta(minutes=1),
                "expires_at": now + timedelta(minutes=5),
            },
            "membership": {
                "membership_id": "membership-a",
                "revision": 1,
                "authorized_at": now,
                "permissions": ["run.reader", "run.contributor", "compiler:query"],
            },
        }
    )


@pytest_asyncio.fixture
async def database():
    configured = os.environ.get("CONTROL_API_TEST_POSTGRES")
    assert configured, "Real PostgreSQL config is required; no silent skip"
    mapping = {
        "Host": "host",
        "Port": "port",
        "Database": "dbname",
        "Username": "user",
        "Password": "password",
    }
    values = dict(item.split("=", 1) for item in configured.split(";") if item)
    synthetic_database = make_conninfo(**{mapping[k]: v for k, v in values.items() if k in mapping})
    schema = f"backend_identity_{uuid.uuid4().hex}"
    async with await psycopg.AsyncConnection.connect(synthetic_database, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped = make_conninfo(synthetic_database, options=f"-c search_path={schema}")
        try:
            async with await psycopg.AsyncConnection.connect(scoped) as conn:
                for migration in MIGRATION_SQL:
                    await conn.execute(migration)
                await conn.execute("""
                    INSERT INTO identity_principals VALUES ('principal-a','https://identity.example.test','same-sub','',true);
                    INSERT INTO identity_workspaces (workspace_id,tenant_id,name)
                    VALUES ('workspace-a','tenant-a','Synthetic A');
                    INSERT INTO identity_memberships
                    VALUES ('membership-a','workspace-a','principal-a','contributor',true);
                    INSERT INTO identity_resource_grants VALUES
                    ('grant-a','workspace-a','membership-a',null,'ontology','catalog-a',1,repeat('a',64),
                     'compiler:query',
                     '{"entity_ids":["e"],"field_ids":["f"],"metric_ids":["m"],"relation_ids":["r"],"member_ids":["v"]}',true);
                    """)
            yield scoped
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def test_membership_and_exact_resource_pin_use_shared_pg_authority(database):
    authority = PostgresAuthorization(database)
    context = context_for()
    await authority.reauthorize(context, "run.reader")
    pin = ResourceVersion(
        contract_version="resource-version/v1",
        scope=context.scope,
        resource_kind="ontology",
        resource_id="catalog-a",
        revision=1,
        content_sha256="a" * 64,
    )
    access = await authority.require(context, pin)
    assert access.entity_ids == frozenset({"e"})
    assert access.field_ids == frozenset({"f"})
    for different in [
        pin.model_copy(update={"revision": 2}),
        pin.model_copy(update={"content_sha256": "b" * 64}),
        pin.model_copy(
            update={"scope": context.scope.model_copy(update={"workspace_id": "workspace-b"})}
        ),
    ]:
        with pytest.raises(AccessDenied):
            await authority.require(context, different)
    async with await psycopg.AsyncConnection.connect(database) as conn:
        await conn.execute("UPDATE identity_resource_grants SET active=false")
    with pytest.raises(AccessDenied):
        await authority.require(context, pin)


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE identity_principals SET active=false",
        "UPDATE identity_memberships SET active=false",
        "UPDATE identity_workspaces SET revision=revision+1",
    ],
)
async def test_revocation_rejects_captured_context_after_authority_restart(database, change):
    context = context_for()
    await PostgresAuthorization(database).reauthorize(context, "run.reader")
    async with await psycopg.AsyncConnection.connect(database) as conn:
        await conn.execute(change)
    with pytest.raises(AccessDenied):
        await PostgresAuthorization(database).reauthorize(context, "run.reader")


async def test_same_subject_other_issuer_or_tenant_never_reuses_membership(database):
    context = context_for()
    forged = [
        context.model_copy(
            update={
                "principal": context.principal.model_copy(
                    update={"issuer": "https://other.example.test"}
                )
            }
        ),
        context.model_copy(
            update={"scope": context.scope.model_copy(update={"tenant_id": "tenant-b"})}
        ),
        context.model_copy(
            update={
                "principal": context.principal.model_copy(
                    update={"principal_id": "other-principal"}
                )
            }
        ),
    ]
    for invalid in forged:
        with pytest.raises(AccessDenied):
            await PostgresAuthorization(database).reauthorize(invalid, "run.reader")


async def test_assertion_replay_is_fenced_across_restarts_and_concurrency(database):
    context = context_for()
    expiry = datetime.now(UTC) + timedelta(seconds=30)

    async def claim():
        try:
            await PostgresAuthorization(database).accept_assertion(
                context, "run.reader", "a" * 32, expiry
            )
            return True
        except AccessDenied:
            return False

    assert sum(await asyncio.gather(*(claim() for _ in range(8)))) == 1
    assert not await claim()
