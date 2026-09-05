"""Input protocols, not authentication. Only the server auth adapter supplies context."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from semantic_api.catalog_v1.models import CompilerFailure, Frozen, ResourceVersion, Scope


class ScopeContext(Protocol):
    @property
    def tenant_id(self) -> str: ...
    @property
    def workspace_id(self) -> str: ...


class PrincipalContext(Protocol):
    @property
    def principal_id(self) -> str: ...
    @property
    def issuer(self) -> str: ...
    @property
    def subject(self) -> str: ...


class AuthenticationContext(Protocol):
    @property
    def method(self) -> str: ...
    @property
    def audience(self) -> str: ...
    @property
    def authenticated_at(self) -> datetime: ...
    @property
    def expires_at(self) -> datetime: ...


class MembershipContext(Protocol):
    @property
    def membership_id(self) -> str: ...
    @property
    def revision(self) -> int: ...
    @property
    def authorized_at(self) -> datetime: ...
    @property
    def permissions(self) -> tuple[str, ...]: ...


class TrustedContext(Protocol):
    @property
    def contract_version(self) -> str: ...
    @property
    def scope(self) -> ScopeContext: ...
    @property
    def principal(self) -> PrincipalContext: ...
    @property
    def authentication(self) -> AuthenticationContext: ...
    @property
    def membership(self) -> MembershipContext: ...


class CatalogAccess(Frozen):
    entity_ids: frozenset[str]
    field_ids: frozenset[str]
    metric_ids: frozenset[str]
    relation_ids: frozenset[str]
    member_ids: frozenset[str]


class Authorization(Protocol):
    async def require(
        self, context: TrustedContext, pin: ResourceVersion, permission: str
    ) -> CatalogAccess:
        """Revalidate issuer+subject, current membership and resource grants server-side."""
        ...


class Owner(Frozen):
    scope: Scope
    principal_id: str
    issuer: str
    subject: str
    membership_id: str
    membership_revision: int


def owner_for(context: TrustedContext | None, pin: ResourceVersion) -> Owner:
    if context is None:
        raise CompilerFailure("TRUSTED_CONTEXT_REQUIRED")
    now = datetime.now(UTC)
    auth = context.authentication
    if (
        context.contract_version != "trusted-context/v1"
        or auth.method != "oidc"
        or not auth.audience
        or not context.principal.issuer.startswith("https://")
        or not context.principal.subject
        or not context.principal.principal_id
        or not context.membership.membership_id
        or context.membership.revision < 1
        or any(
            t.tzinfo is None
            for t in (auth.authenticated_at, auth.expires_at, context.membership.authorized_at)
        )
    ):
        raise CompilerFailure("TRUSTED_CONTEXT_INVALID")
    if (
        auth.authenticated_at > now
        or auth.expires_at <= now
        or context.membership.authorized_at > now
    ):
        raise CompilerFailure("AUTHORIZATION_EXPIRED")
    scope = Scope(tenant_id=context.scope.tenant_id, workspace_id=context.scope.workspace_id)
    if scope != pin.scope:
        raise CompilerFailure("RESOURCE_NOT_AVAILABLE")
    return Owner(
        scope=scope,
        principal_id=context.principal.principal_id,
        issuer=context.principal.issuer,
        subject=context.principal.subject,
        membership_id=context.membership.membership_id,
        membership_revision=context.membership.revision,
    )
