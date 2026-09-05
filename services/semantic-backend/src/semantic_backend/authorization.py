from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from psycopg import AsyncConnection
from psycopg import Error as PostgresError

from semantic_backend.auth_context import AccessDenied, ResourceVersion, TrustedContext

AUTHORIZATION_LOCK = 731320032


@dataclass(frozen=True)
class CatalogAccess:
    entity_ids: frozenset[str]
    field_ids: frozenset[str]
    metric_ids: frozenset[str]
    relation_ids: frozenset[str]
    member_ids: frozenset[str]


class MembershipAuthority(Protocol):
    async def reauthorize(self, context: TrustedContext, permission: str) -> None: ...

    def guard(
        self,
        context: TrustedContext,
        pin: ResourceVersion | None = None,
        permission: str = "run.reader",
        *,
        deadline: float | None = None,
    ) -> AbstractAsyncContextManager[AuthorizationDecision]: ...


@dataclass(frozen=True)
class AuthorizationDecision:
    connection: AsyncConnection[tuple[object, ...]]
    access: CatalogAccess | None


class PostgresAuthorization:
    def __init__(self, connection_string: str) -> None:
        if not connection_string:
            raise ValueError("SEMANTIC_NEXUS_IDENTITY_POSTGRES is required")
        self._connection_string = connection_string
        self._connections = asyncio.Semaphore(8)

    async def ready(self) -> bool:
        try:
            async with asyncio.timeout(5), self._connections:
                async with await AsyncConnection.connect(
                    self._connection_string, connect_timeout=5
                ) as conn:
                    cursor = await conn.execute(
                        "SELECT 1 FROM control_schema_versions WHERE version = 2"
                    )
                    return await cursor.fetchone() is not None
        except (PostgresError, TimeoutError):
            return False

    async def _check(
        self,
        connection: AsyncConnection[tuple[object, ...]],
        context: TrustedContext,
        permission: str,
    ) -> None:
        await connection.execute("SELECT pg_advisory_xact_lock_shared(%s)", (AUTHORIZATION_LOCK,))
        if context.authentication.expires_at <= datetime.now(UTC):
            raise AccessDenied("Current workspace access is not authorized.")
        cursor = await connection.execute(
            """SELECT 1 FROM identity_access
               WHERE principal_id = %s AND issuer = %s AND subject = %s
                 AND tenant_id = %s AND workspace_id = %s AND membership_id = %s
                 AND revision = %s AND %s = ANY(permissions)""",
            (
                context.principal.principal_id,
                context.principal.issuer,
                context.principal.subject,
                context.scope.tenant_id,
                context.scope.workspace_id,
                context.membership.membership_id,
                context.membership.revision,
                permission,
            ),
        )
        if await cursor.fetchone() is None:
            raise AccessDenied("Current workspace access is not authorized.")

    async def reauthorize(self, context: TrustedContext, permission: str) -> None:
        async with self.guard(context, permission=permission):
            pass

    async def accept_assertion(
        self, context: TrustedContext, permission: str, assertion_id: str, expires_at: datetime
    ) -> None:
        async with self.guard(context, permission=permission) as decision:
            conn = decision.connection
            await conn.execute("DELETE FROM identity_assertion_uses WHERE expires_at < now()")
            cursor = await conn.execute(
                """INSERT INTO identity_assertion_uses (assertion_id, expires_at) VALUES (%s,%s)
                   ON CONFLICT DO NOTHING RETURNING assertion_id""",
                (assertion_id, expires_at),
            )
            if await cursor.fetchone() is None:
                raise AccessDenied("Service assertion has already been used.")

    async def require(
        self, context: TrustedContext, pin: ResourceVersion, permission: str = "compiler:query"
    ) -> CatalogAccess:
        async with self.guard(context, pin, permission) as decision:
            assert decision.access is not None
            return decision.access

    @asynccontextmanager
    async def guard(
        self,
        context: TrustedContext,
        pin: ResourceVersion | None = None,
        permission: str = "run.reader",
        *,
        deadline: float | None = None,
    ) -> AsyncIterator[AuthorizationDecision]:
        loop = asyncio.get_running_loop()
        remaining = min(
            5.0, (context.authentication.expires_at - datetime.now(UTC)).total_seconds()
        )
        if deadline is not None:
            remaining = min(remaining, deadline - loop.time())
        if remaining <= 0:
            raise AccessDenied("Authorization expired before transaction entry.")
        async with asyncio.timeout(remaining) as budget, self._connections:
            async with await AsyncConnection.connect(
                self._connection_string,
                connect_timeout=max(1, math.ceil(remaining)),
                autocommit=True,
            ) as conn:
                async with conn.transaction():
                    await conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    await conn.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (f"{max(1, int(remaining * 1000))}ms",),
                    )
                    # First lock: all cooperating identity mutations take its exclusive counterpart.
                    await self._check(conn, context, permission)
                    access = (
                        await self._resource_access(conn, context, pin, permission)
                        if pin is not None
                        else None
                    )
                    yield AuthorizationDecision(conn, access)
                    # Wall clock: a row lock wait may outlive the token.
                    if context.authentication.expires_at <= datetime.now(UTC):
                        raise AccessDenied("Authorization expired before commit.")
                    if deadline is not None and deadline <= loop.time():
                        raise TimeoutError(
                            "Authorization transaction exceeded the caller deadline."
                        )
                    if budget.expired():
                        raise TimeoutError("Authorization transaction exceeded its bounded budget.")

    async def _resource_access(
        self,
        conn: AsyncConnection[tuple[object, ...]],
        context: TrustedContext,
        pin: ResourceVersion,
        permission: str,
    ) -> CatalogAccess:
        if (pin.scope.tenant_id, pin.scope.workspace_id) != (
            context.scope.tenant_id,
            context.scope.workspace_id,
        ):
            raise AccessDenied("Resource access is not authorized.")
        cursor = await conn.execute(
            """SELECT r.allowed_ids FROM identity_resource_grants r
                   WHERE r.workspace_id = %s AND r.resource_kind = %s AND r.resource_id = %s
                     AND r.revision = %s AND r.content_sha256 = %s
                     AND r.permission = %s AND r.active
                     AND (r.membership_id = %s OR r.group_id IN (
                         SELECT g.group_id FROM identity_groups g
                         JOIN identity_group_members gm USING (workspace_id, group_id)
                         WHERE g.workspace_id = %s AND g.active AND gm.membership_id = %s))""",
            (
                pin.scope.workspace_id,
                pin.resource_kind,
                pin.resource_id,
                pin.revision,
                pin.content_sha256,
                permission,
                context.membership.membership_id,
                context.scope.workspace_id,
                context.membership.membership_id,
            ),
        )
        rows = await cursor.fetchall()
        if not rows:
            raise AccessDenied("Resource access is not authorized.")
        names = ("entity_ids", "field_ids", "metric_ids", "relation_ids", "member_ids")
        values: dict[str, set[str]] = {name: set() for name in names}
        for row in rows:
            document = row[0]
            if not isinstance(document, dict) or set(document) != set(names):
                raise AccessDenied("Invalid stored resource grant.")
            for name in names:
                items = document[name]
                if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                    raise AccessDenied("Invalid stored resource grant.")
                values[name].update(items)
        return CatalogAccess(*(frozenset(values[name]) for name in names))
