from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from psycopg import AsyncConnection

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


class PostgresAuthorization:
    def __init__(self, connection_string: str) -> None:
        if not connection_string:
            raise ValueError("SEMANTIC_NEXUS_IDENTITY_POSTGRES is required")
        self._connection_string = connection_string

    async def _check(
        self,
        connection: AsyncConnection[tuple[object, ...]],
        context: TrustedContext,
        permission: str,
    ) -> None:
        if context.authentication.expires_at <= datetime.now(UTC):
            raise AccessDenied("Current workspace access is not authorized.")
        await connection.execute("SELECT pg_advisory_xact_lock_shared(%s)", (AUTHORIZATION_LOCK,))
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
        async with await AsyncConnection.connect(
            self._connection_string, connect_timeout=5
        ) as conn:
            await self._check(conn, context, permission)

    async def accept_assertion(
        self, context: TrustedContext, permission: str, assertion_id: str, expires_at: datetime
    ) -> None:
        async with await AsyncConnection.connect(
            self._connection_string, connect_timeout=5
        ) as conn:
            await self._check(conn, context, permission)
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
        if (pin.scope.tenant_id, pin.scope.workspace_id) != (
            context.scope.tenant_id,
            context.scope.workspace_id,
        ):
            raise AccessDenied("Resource access is not authorized.")
        async with await AsyncConnection.connect(
            self._connection_string, connect_timeout=5
        ) as conn:
            await self._check(conn, context, permission)
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
