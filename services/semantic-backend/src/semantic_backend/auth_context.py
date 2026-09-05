from __future__ import annotations

import os
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[
    str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$", max_length=128)
]


class AccessDenied(PermissionError):
    pass


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkspaceScope(FrozenModel):
    tenant_id: Identifier
    workspace_id: Identifier


class TrustedPrincipal(FrozenModel):
    principal_id: Identifier
    issuer: Annotated[str, StringConstraints(pattern=r"^https://[^\s?#@]+$", max_length=2048)]
    subject: Annotated[str, StringConstraints(pattern=r"^\S+$", max_length=255)]


class VerifiedAuthentication(FrozenModel):
    method: Literal["oidc"]
    audience: str = Field(min_length=1, max_length=256)
    authenticated_at: AwareDatetime
    expires_at: AwareDatetime


class AuthorizedMembership(FrozenModel):
    membership_id: Identifier
    revision: int = Field(ge=1, strict=True)
    authorized_at: AwareDatetime
    permissions: tuple[Identifier, ...] = Field(min_length=1)


class TrustedContext(FrozenModel):
    contract_version: Literal["trusted-context/v1"]
    scope: WorkspaceScope
    principal: TrustedPrincipal
    authentication: VerifiedAuthentication
    membership: AuthorizedMembership

    @model_validator(mode="after")
    def ordered_times(self) -> TrustedContext:
        if not (
            self.authentication.authenticated_at
            <= self.membership.authorized_at
            < self.authentication.expires_at
        ) or len(set(self.membership.permissions)) != len(self.membership.permissions):
            raise ValueError("Invalid trusted context ordering or permissions")
        return self


class ResourceVersion(FrozenModel):
    contract_version: Literal["resource-version/v1"]
    scope: WorkspaceScope
    resource_kind: Literal[
        "ontology",
        "knowledge-base",
        "resolver",
        "credential-reference",
        "llm",
        "prompt-template",
        "compute-engine",
        "result-store",
        "run",
        "dataset",
        "user",
        "group",
        "workspace",
        "policy",
    ]
    resource_id: Identifier
    revision: int = Field(ge=1, strict=True)
    content_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


request_context: ContextVar[TrustedContext | None] = ContextVar("nexus_context", default=None)


def get_trusted_context() -> TrustedContext:
    context = request_context.get()
    if context is None or context.authentication.expires_at <= datetime.now(UTC):
        raise AccessDenied("Current workspace access is not authorized.")
    return context


def legacy_development() -> bool:
    mode = os.environ.get("SEMANTIC_NEXUS_AUTH_MODE", "service")
    if mode not in {"service", "legacy-development"}:
        raise ValueError("SEMANTIC_NEXUS_AUTH_MODE must be service or legacy-development")
    if mode == "legacy-development":
        if os.environ.get("SEMANTIC_NEXUS_ENVIRONMENT") != "Development":
            raise ValueError("Legacy authentication requires explicit Development environment")
        return True
    return False
