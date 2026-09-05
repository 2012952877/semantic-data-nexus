from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from conftest import request_for
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from semantic_backend.auth_context import (
    AccessDenied,
    TrustedContext,
    get_trusted_context,
    legacy_development,
    request_context,
)
from semantic_backend.auth_middleware import SERVICE_AUDIENCE, SERVICE_ISSUER, ServiceAuthentication
from semantic_backend.authorization import PostgresAuthorization
from semantic_backend.models import RunState, RunStatus
from semantic_backend.repository import InMemoryRunRepository, RunNotFoundError


def context_for(
    workspace: str = "workspace-a", issuer: str = "https://identity.example.test"
) -> TrustedContext:
    now = datetime.now(UTC)
    return TrustedContext.model_validate(
        {
            "contract_version": "trusted-context/v1",
            "scope": {"tenant_id": f"tenant-{workspace}", "workspace_id": workspace},
            "principal": {
                "principal_id": f"principal-{workspace}",
                "issuer": issuer,
                "subject": "same-sub",
            },
            "authentication": {
                "method": "oidc",
                "audience": "nexus-api",
                "authenticated_at": now - timedelta(minutes=1),
                "expires_at": now + timedelta(minutes=5),
            },
            "membership": {
                "membership_id": f"member-{workspace}",
                "revision": 1,
                "authorized_at": now,
                "permissions": ["run.reader", "run.contributor"],
            },
        }
    )


class TestAuthority(PostgresAuthorization):
    __test__ = False

    def __init__(self) -> None:
        super().__init__("unused-synthetic-test")
        self.active = True
        self.used: set[str] = set()

    async def reauthorize(self, context: TrustedContext, permission: str) -> None:
        if not self.active or permission not in context.membership.permissions:
            raise AccessDenied("Access revoked")

    async def accept_assertion(
        self, context: TrustedContext, permission: str, assertion_id: str, expires_at: datetime
    ) -> None:
        await self.reauthorize(context, permission)
        if assertion_id in self.used:
            raise AccessDenied("Replay")
        self.used.add(assertion_id)


@pytest.fixture
def signed_boundary():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    authority = TestAuthority()
    app = FastAPI()
    app.add_middleware(
        ServiceAuthentication,
        authority=authority,
        public_key=key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )

    @app.get("/v1/runs/{run_id}")
    async def detail(run_id: str):
        return {"run_id": run_id, "workspace": get_trusted_context().scope.workspace_id}

    @app.post("/v1/runs")
    async def start(request: Request):
        return {"body": await request.json(), "workspace": get_trusted_context().scope.workspace_id}

    @app.get("/v1/revoke-during-read")
    async def revoke():
        authority.active = False
        return {"must_not_escape": "synthetic-result"}

    def token(path="/v1/runs/one", method="GET", body=b"", overrides=None, header=None):
        now = datetime.now(UTC)
        claims = {
            "iss": SERVICE_ISSUER,
            "aud": SERVICE_AUDIENCE,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=30)).timestamp()),
            "jti": "a" * 32,
            "ctx": context_for().model_dump(mode="json"),
            "htm": method,
            "htu": path,
            "bh": hashlib.sha256(body).hexdigest(),
        }
        claims.update(overrides or {})
        return jwt.encode(
            claims, key, algorithm="RS256", headers=header or {"typ": "nexus-service+jwt"}
        )

    return app, authority, token


async def test_authenticated_service_request_binds_scope_and_is_single_use(signed_boundary):
    app, _, token = signed_boundary
    headers = {"Authorization": f"Bearer {token()}", "X-Workspace-Id": "forged"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://backend.test"
    ) as client:
        result = await client.get("/v1/runs/one", headers=headers)
        assert result.status_code == 200
        assert result.json()["workspace"] == "workspace-a"
        assert (await client.get("/v1/runs/one", headers=headers)).status_code == 403
        direct = await client.get(
            "/v1/runs/one", headers={"X-Trusted-Context": context_for().model_dump_json()}
        )
        assert direct.status_code == 401
    with pytest.raises(AccessDenied):
        get_trusted_context()


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://unregistered.example.test"},
        {"aud": "nexus-browser"},
        {"exp": 1},
        {"nbf": 9999999999},
        {"htm": "POST"},
        {"htu": "/v1/runs/other"},
        {"bh": "b" * 64},
        {"ctx": {"scope": {"workspace_id": "forged"}}},
    ],
)
async def test_invalid_assertions_are_denied(signed_boundary, overrides):
    app, _, token = signed_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://backend.test"
    ) as client:
        result = await client.get(
            "/v1/runs/one", headers={"Authorization": f"Bearer {token(overrides=overrides)}"}
        )
        assert result.status_code == 403


async def test_oidc_tokens_are_not_service_assertions(signed_boundary):
    app, _, token = signed_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://backend.test"
    ) as client:
        result = await client.get(
            "/v1/runs/one", headers={"Authorization": f"Bearer {token(header={'typ': 'JWT'})}"}
        )
        assert result.status_code == 403


async def test_body_binding_and_revocation_before_result_release(signed_boundary):
    app, authority, token = signed_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://backend.test"
    ) as client:
        altered = await client.post(
            "/v1/runs",
            content=b'{"scope":"forged"}',
            headers={"Authorization": f"Bearer {token('/v1/runs', 'POST', b'{}')}"},
        )
        assert altered.status_code == 403
        revoked = await client.get(
            "/v1/revoke-during-read",
            headers={"Authorization": f"Bearer {token('/v1/revoke-during-read')}"},
        )
        assert revoked.status_code == 403
        assert "must_not_escape" not in revoked.text
        assert not authority.active


async def test_repository_scopes_every_lookup_and_list(monkeypatch):
    monkeypatch.setenv("SEMANTIC_NEXUS_AUTH_MODE", "service")
    repository = InMemoryRunRepository()
    a, b = context_for(), context_for("workspace-b")
    token = request_context.set(a)
    try:
        request = request_for("run_" + "a" * 32)
        status = RunStatus(run_id=request.run_id, state=RunState.STARTING)
        record, created = await repository.create(request, status, context=a)
        assert created and record.trusted_context == a
        request_context.set(b)
        assert await repository.list_records(context=b) == ()
        with pytest.raises(RunNotFoundError):
            await repository.get(request.run_id, context=b)
        with pytest.raises(RunNotFoundError):
            await repository.create(request, status, context=b)
        request_context.set(None)
        with pytest.raises(AccessDenied):
            await repository.get(request.run_id)
    finally:
        request_context.reset(token)


def test_default_mode_is_fail_closed_and_legacy_requires_development(monkeypatch):
    monkeypatch.delenv("SEMANTIC_NEXUS_AUTH_MODE")
    assert not legacy_development()
    monkeypatch.setenv("SEMANTIC_NEXUS_AUTH_MODE", "legacy-development")
    monkeypatch.setenv("SEMANTIC_NEXUS_ENVIRONMENT", "Production")
    with pytest.raises(ValueError, match="Development"):
        legacy_development()
