"""Typed contract conversion into the existing reviewed backend authority."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from semantic_api.catalog_v1.guard import AuthorizationDecision
from semantic_api.catalog_v1.models import ResourceVersion
from semantic_api.catalog_v1.trust import CatalogAccess, TrustedContext

from semantic_backend.auth_context import AccessDenied, WorkspaceScope
from semantic_backend.auth_context import ResourceVersion as BackendResourceVersion
from semantic_backend.auth_context import TrustedContext as BackendTrustedContext
from semantic_backend.authorization import PostgresAuthorization
from semantic_backend.catalog_disconnect import require_connected


class CatalogAuthorization:
    def __init__(self, authority: PostgresAuthorization) -> None:
        self.authority = authority

    @asynccontextmanager
    async def guard(
        self,
        context: TrustedContext,
        pin: ResourceVersion | None = None,
        permission: str = "run.reader",
        *,
        deadline: float | None = None,
    ) -> AsyncIterator[AuthorizationDecision]:
        require_connected()
        if not isinstance(context, BackendTrustedContext):
            raise AccessDenied("A verified backend context is required.")
        converted = (
            None
            if pin is None
            else BackendResourceVersion(
                contract_version=pin.contract_version,
                scope=WorkspaceScope(
                    tenant_id=pin.scope.tenant_id, workspace_id=pin.scope.workspace_id
                ),
                resource_kind=pin.resource_kind,
                resource_id=pin.resource_id,
                revision=pin.revision,
                content_sha256=pin.content_sha256,
            )
        )
        async with self.authority.guard(
            context, converted, permission, deadline=deadline
        ) as decision:
            require_connected()
            yield decision
            require_connected()

    async def require(
        self, context: TrustedContext, pin: ResourceVersion, permission: str = "compiler:query"
    ) -> CatalogAccess:
        async with self.guard(context, pin, permission) as decision:
            value = decision.access
            if value is None:
                raise AccessDenied("Resource access is not authorized.")
            result = CatalogAccess(
                entity_ids=value.entity_ids,
                field_ids=value.field_ids,
                metric_ids=value.metric_ids,
                relation_ids=value.relation_ids,
                member_ids=value.member_ids,
            )
        return result
