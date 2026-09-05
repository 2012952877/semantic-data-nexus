"""Real shared-authority PostgreSQL and signed HTTP catalog composition."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import psycopg
import pyarrow as pa
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from postgres_authorization import context_for
from postgres_authorization import database as database
from query_runtime.domain import BoundSource, CapabilityCatalog, OperatorKind
from query_runtime.resolver import FakeResolver
from semantic_api.catalog_v1.catalog import fingerprint, pin_for
from semantic_api.catalog_v1.models import CatalogDocument
from semantic_api.catalog_v1.provider import CatalogHTTPProvider
from semantic_api.compiler import SemanticCompiler
from semantic_api.ontology import OntologyRegistry
from semantic_api.provider_config import ProviderSettings

from semantic_backend.api import create_app
from semantic_backend.auth_context import TrustedContext
from semantic_backend.authorization import PostgresAuthorization
from semantic_backend.catalog_compilation import TYPES, CatalogBindings, EntityBinding, FieldBinding
from semantic_backend.catalog_configuration import (
    CatalogConfiguration,
    CatalogEntry,
    CatalogRegistry,
    SyntheticCatalogResolver,
)
from semantic_backend.catalog_service import CatalogQueryService
from semantic_backend.service import OrchestrationService

ROOT = Path(__file__).resolve().parents[3]
CASES = json.loads((ROOT / "contracts/compiler/v1/examples.json").read_text())["cases"]


class ObservedAuthority(PostgresAuthorization):
    def __init__(self, database):
        super().__init__(database)
        self.pause_publication = False
        self.publication_entered = asyncio.Event()
        self.publication_release = asyncio.Event()
        self.publication_pid = None

    @asynccontextmanager
    async def guard(self, context, pin=None, permission="run.reader", *, deadline=None):
        async with super().guard(context, pin, permission, deadline=deadline) as decision:
            yield decision
            if self.pause_publication and not self.publication_entered.is_set():
                row = await (
                    await decision.connection.execute(
                        "SELECT 1 FROM compiler_catalog_results_v1 WHERE state='completed' LIMIT 1"
                    )
                ).fetchone()
                if row:
                    self.publication_pid = decision.connection.info.backend_pid
                    self.publication_entered.set()
                    await self.publication_release.wait()


def prepared_case(index, context):
    case = copy.deepcopy(CASES[index])
    case["catalog"]["scope"] = context.scope.model_dump(mode="json")
    document = CatalogDocument.model_validate(case["catalog"])
    pin = pin_for(document)
    case["candidate"]["graph"]["catalog"] = pin.model_dump(mode="json")
    fields = {f.id: f for f in document.fields}
    bindings = CatalogBindings(
        contract_version="catalog-bindings/v1",
        catalog=pin,
        entities=tuple(
            EntityBinding(
                entity_id=item["entity_id"],
                source=BoundSource(
                    alias=item["alias"],
                    source_type=item["source_type"],
                    object_name=item["object_name"],
                ),
                fields=tuple(
                    FieldBinding(field_id=f, column_name=c, data_type=TYPES[fields[f].data_type])
                    for f, c in item["columns"].items()
                ),
            )
            for item in case["bindings"]
        ),
    )
    return case, CatalogEntry(document=document, bindings=bindings, rows=case["rows"])


@asynccontextmanager
async def model_server(candidates):
    requests = []
    active = set()
    entered = asyncio.Event()
    release = asyncio.Event()
    state = {"pause": False}

    async def handle(reader, writer):
        task = asyncio.current_task()
        active.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            headers = dict(line.split(": ", 1) for line in head.decode().split("\r\n")[1:] if line)
            payload = json.loads(await reader.readexactly(int(headers["Content-Length"])))
            requests.append(payload)
            envelope = json.loads(payload["messages"][1]["content"])
            candidate = candidates[envelope["context"]["catalog"]["resource_id"]]
            entered.set()
            if state["pause"]:
                await release.wait()
            body = json.dumps(
                {
                    "id": "synthetic-public",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "synthetic-public-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": json.dumps(candidate)},
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
                }
            ).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n")
            writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            active.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    provider = CatalogHTTPProvider(
        ProviderSettings(
            mode="openai_compatible",
            model="synthetic-public-model",
            endpoint=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/chat/completions",
            credential_env="SYNTHETIC_CATALOG_KEY",
            allow_local_mock=True,
        ),
        "synthetic-model-key",
    )
    try:
        async with server:
            yield provider, requests, state, entered, release
    finally:
        release.set()
        await provider.aclose()
        await asyncio.gather(*active, return_exceptions=True)


@pytest_asyncio.fixture
async def public_case(database, monkeypatch, request):
    monkeypatch.setenv("SEMANTIC_NEXUS_AUTH_MODE", "service")
    monkeypatch.setenv("SEMANTIC_NEXUS_ENVIRONMENT", "Development")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv(
        "SEMANTIC_NEXUS_SERVICE_PUBLIC_KEY",
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
    )
    context = context_for()
    if hasattr(request, "param"):
        integer = request.param
        case, entry = integer_case(integer, context)
        cases, entries = (case,), (entry,)
    else:
        cases, entries = zip(*(prepared_case(index, context) for index in range(2)), strict=True)
    registry = CatalogRegistry(
        CatalogConfiguration(contract_version="catalog-server/v1", entries=entries)
    )
    async with await psycopg.AsyncConnection.connect(database) as connection:
        for entry in entries:
            document = entry.document
            allowed = {
                "entity_ids": [e.id for e in document.entities],
                "field_ids": [f.id for f in document.fields],
                "metric_ids": [m.id for m in document.metrics],
                "relation_ids": [r.id for r in document.relations],
                "member_ids": [m.id for f in document.fields for m in f.members],
            }
            await connection.execute(
                """INSERT INTO identity_resource_grants
                   VALUES (%s,'workspace-a','membership-a',null,'ontology',%s,%s,%s,
                           'compiler:query',%s::jsonb,true)""",
                (
                    "grant-" + document.resource_id,
                    document.resource_id,
                    document.revision,
                    pin_for(document).content_sha256,
                    json.dumps(allowed),
                ),
            )
        await connection.execute("""
            INSERT INTO identity_workspaces(workspace_id,tenant_id,name)
            VALUES('workspace-b','tenant-b','Synthetic B');
            INSERT INTO identity_memberships VALUES
            ('membership-b','workspace-b','principal-a','contributor',true)
        """)
    candidates = {
        entry.document.resource_id: case["candidate"]
        for entry, case in zip(entries, cases, strict=True)
    }
    async with model_server(candidates) as (provider, requests, state, entered, release):
        authority = ObservedAuthority(database)
        service = CatalogQueryService(
            registry=registry, provider=provider, authority=authority, database=database
        )
        legacy_resolver = FakeResolver(
            tables={"unused": pa.table({"x": [1]})},
            catalogs={
                "unused": CapabilityCatalog(
                    source_alias="unused",
                    source_type="synthetic",
                    operator_kinds=frozenset({OperatorKind.SELECT}),
                )
            },
        )
        orchestrator = OrchestrationService(
            authority=authority,
            resolver=legacy_resolver,
            compiler=SemanticCompiler(OntologyRegistry.load_default()),
        )
        app = create_app(orchestrator, service)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://backend"
            ) as client:
                yield {
                    "client": client,
                    "key": key,
                    "context": context,
                    "cases": cases,
                    "entries": entries,
                    "calls": requests,
                    "database": database,
                    "state": state,
                    "entered": entered,
                    "release": release,
                    "authority": authority,
                    "app": app,
                    "service": service,
                    "provider": provider,
                }


def query_payload(case):
    return {
        "contract_version": "catalog-compile/v1",
        "request_id": "synthetic-public-request",
        "catalog": case["candidate"]["graph"]["catalog"],
        "question": case["question"],
    }


def signed_parts(case, path, payload, context=None, token=None):
    context = context or case["context"]
    body = json.dumps(payload, separators=(",", ":")).encode()
    now = int(time.time())
    assertion = token or jwt.encode(
        {
            "iss": "https://control.example.test",
            "aud": "semantic-backend",
            "iat": now,
            "nbf": now,
            "exp": min(now + 30, int(context.authentication.expires_at.timestamp())),
            "jti": uuid4().hex,
            "htm": "POST",
            "htu": path,
            "bh": hashlib.sha256(body).hexdigest(),
            "ctx": context.model_dump(mode="json"),
        },
        case["key"],
        algorithm="RS256",
        headers={"typ": "nexus-service+jwt"},
    )
    return body, {
        "Authorization": "Bearer " + assertion,
        "Content-Type": "application/json",
    }


async def signed(case, path, payload, context=None, token=None):
    body, headers = signed_parts(case, path, payload, context, token)
    return await case["client"].post(path, content=body, headers=headers)


async def start_signed_asgi(case, path, payload):
    body, headers = signed_parts(case, path, payload)
    incoming = asyncio.Queue()
    midpoint = len(body) // 2
    await incoming.put({"type": "http.request", "body": body[:midpoint], "more_body": True})
    await incoming.put({"type": "http.request", "body": body[midpoint:], "more_body": False})
    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "server": ("backend", 80),
        "client": ("127.0.0.1", 40000),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return asyncio.create_task(case["app"](scope, incoming.get, send)), incoming, sent


def integer_case(value, context):
    binding = {
        "entity_id": "integer.sample",
        "alias": "integers",
        "source_type": "synthetic",
        "object_name": "synthetic_integers",
        "columns": {"integer.sample.value": "value"},
        "utc_naive_fields": [],
    }
    document = CatalogDocument.model_validate(
        {
            "contract_version": "compiler-catalog/v1",
            "scope": context.scope.model_dump(mode="json"),
            "resource_id": "integer-catalog",
            "revision": 1,
            "bindings_sha256": fingerprint(
                {"contract_version": "catalog-binding-content/v1", "entities": [binding]}
            ),
            "entities": [{"id": "integer.sample", "label": "Samples"}],
            "fields": [
                {
                    "id": "integer.sample.value",
                    "label": "Value",
                    "entity_id": "integer.sample",
                    "data_type": "integer",
                }
            ],
        }
    )
    pin = pin_for(document)
    candidate = {
        "contract_version": "compiler-candidate/v1",
        "status": "graph",
        "ambiguity_id": None,
        "graph": {
            "contract_version": "sqg/v1",
            "catalog": pin.model_dump(mode="json"),
            "nodes": [
                {
                    "id": "select",
                    "dependencies": [],
                    "operation": {
                        "kind": "SELECT",
                        "entity_id": "integer.sample",
                        "columns": ["integer.sample.value"],
                    },
                },
                {
                    "id": "project",
                    "dependencies": ["select"],
                    "operation": {
                        "kind": "PROJECT",
                        "columns": [{"source": "integer.sample.value", "alias": "value"}],
                    },
                },
            ],
            "output_node_id": "project",
            "result_schema": [{"name": "value", "data_type": "integer"}],
        },
    }
    bindings = CatalogBindings(
        contract_version="catalog-bindings/v1",
        catalog=pin,
        entities=(
            EntityBinding(
                entity_id="integer.sample",
                source=BoundSource(
                    alias="integers", source_type="synthetic", object_name="synthetic_integers"
                ),
                fields=(
                    FieldBinding(
                        field_id="integer.sample.value",
                        column_name="value",
                        data_type=TYPES["integer"],
                    ),
                ),
            ),
        ),
    )
    return {"candidate": candidate, "question": "Value"}, CatalogEntry(
        document=document,
        bindings=bindings,
        rows={"integers": [{"value": value}]},
    )


@pytest.mark.parametrize("domain", [0, 1])
async def test_signed_public_catalog_real_authority_model_runtime_and_cached_result(
    public_case, domain
):
    case = public_case
    payload = query_payload(case["cases"][domain])
    response = await signed(case, "/v1/catalog/queries", payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "succeeded"
    columns = result["result"]["columns"]
    rows = [
        dict(zip((c["key"] for c in columns), row, strict=True)) for row in result["result"]["rows"]
    ]
    assert rows == case["cases"][domain]["expected"]
    assert result["provenance"]["runtime_contract"] == "query-runtime/v1"
    assert result["compilation"]["graph"] == case["cases"][domain]["candidate"]["graph"]
    assert result["compilation"]["input_tokens"] == 100
    replay = await signed(case, "/v1/catalog/queries", payload)
    assert replay.json() == result
    assert len(case["calls"]) == 1


async def test_signed_public_clarification_resume_conflict_and_current_revoke(public_case):
    case = public_case
    payload = query_payload(case["cases"][1])
    payload["question"] = "yield for alpha above score 1"
    response = await signed(case, "/v1/catalog/queries", payload)
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["status"] == "clarification_required" and not case["calls"]
    identifier = first["compilation"]["clarification_id"]
    path = f"/v1/catalog/clarifications/{identifier}/answers"
    answer = {
        "contract_version": "catalog-answer/v1",
        "catalog": payload["catalog"],
        "revision": 1,
        "choice_id": "lab.mean_yield",
    }
    done = await signed(case, path, answer)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "succeeded"
    assert (await signed(case, path, answer)).json() == done.json()
    assert (await signed(case, path, {**answer, "choice_id": "lab.total_yield"})).status_code == 409
    async with await psycopg.AsyncConnection.connect(case["database"]) as connection:
        await connection.execute("SELECT pg_advisory_xact_lock(731320032)")
        await connection.execute(
            "UPDATE identity_resource_grants SET active=false WHERE resource_id=%s",
            (payload["catalog"]["resource_id"],),
        )
    assert (await signed(case, path, answer)).status_code == 403
    assert len(case["calls"]) == 1


async def test_public_catalog_rejects_anonymous_foreign_scope_and_tampered_assertion(public_case):
    case = public_case
    payload = query_payload(case["cases"][0])
    assert (await case["client"].post("/v1/catalog/queries", json=payload)).status_code == 401
    foreign = case["context"].model_dump(mode="json")
    foreign["scope"] = {"tenant_id": "tenant-b", "workspace_id": "workspace-b"}
    foreign["membership"]["membership_id"] = "membership-b"
    assert (
        await signed(case, "/v1/catalog/queries", payload, TrustedContext.model_validate(foreign))
    ).status_code == 403
    assert (
        await signed(case, "/v1/catalog/queries", payload, token="synthetic-invalid")
    ).status_code == 403
    assert not case["calls"]


async def test_public_catalog_revoke_cancels_inflight_model_and_blocks_commit(public_case):
    case = public_case
    case["state"]["pause"] = True
    payload = query_payload(case["cases"][1])
    task = asyncio.create_task(signed(case, "/v1/catalog/queries", payload))
    await asyncio.wait_for(case["entered"].wait(), 2)
    try:
        async with await psycopg.AsyncConnection.connect(case["database"]) as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(731320032)")
            await connection.execute(
                "UPDATE identity_resource_grants SET active=false WHERE resource_id=%s",
                (payload["catalog"]["resource_id"],),
            )
        response = await asyncio.wait_for(task, 2)
        assert response.status_code == 403
        assert not case["release"].is_set()
        async with await psycopg.AsyncConnection.connect(case["database"]) as connection:
            row = await (
                await connection.execute(
                    "SELECT payload FROM compiler_clarifications_v1 WHERE request_id=%s",
                    (payload["request_id"],),
                )
            ).fetchone()
            assert json.loads(row[0])["current"] is None
            assert (
                await (
                    await connection.execute("SELECT count(*) FROM compiler_catalog_results_v1")
                ).fetchone()
            )[0] == 0
        assert (await signed(case, "/v1/catalog/queries", payload)).status_code == 403
        assert len(case["calls"]) == 1
    finally:
        case["release"].set()
        await asyncio.gather(task, return_exceptions=True)


async def test_public_commit_blocks_revoker_until_publication(public_case):
    case = public_case
    case["authority"].pause_publication = True
    payload = query_payload(case["cases"][1])
    task = asyncio.create_task(signed(case, "/v1/catalog/queries", payload))
    await asyncio.wait_for(case["authority"].publication_entered.wait(), 3)
    async with await psycopg.AsyncConnection.connect(case["database"]) as revoker:
        revoke = asyncio.create_task(revoker.execute("SELECT pg_advisory_xact_lock(731320032)"))
        try:
            async with asyncio.timeout(2):
                async with await psycopg.AsyncConnection.connect(case["database"]) as observer:
                    while True:
                        blockers = (
                            await (
                                await observer.execute(
                                    "SELECT pg_blocking_pids(%s)", (revoker.info.backend_pid,)
                                )
                            ).fetchone()
                        )[0]
                        if blockers:
                            assert case["authority"].publication_pid in blockers
                            break
            assert not task.done() and not revoke.done()
            case["authority"].publication_release.set()
            await revoke
            await revoker.execute(
                "UPDATE identity_resource_grants SET active=false WHERE resource_id=%s",
                (payload["catalog"]["resource_id"],),
            )
        finally:
            case["authority"].publication_release.set()
            await asyncio.gather(revoke, return_exceptions=True)
    response = await task
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"
    assert (await signed(case, "/v1/catalog/queries", payload)).status_code == 403


@pytest.mark.parametrize(
    "public_case",
    [
        9_007_199_254_740_990,
        9_007_199_254_740_991,
        9_007_199_254_740_992,
        -9_007_199_254_740_991,
        -9_007_199_254_740_992,
        -(2**63),
        2**63 - 1,
    ],
    indirect=True,
)
async def test_public_integer_boundaries_are_exact_or_durable_typed_failures(public_case):
    case = public_case
    payload = query_payload(case["cases"][0])
    value = case["entries"][0].rows["integers"][0]["value"]
    first = await signed(case, "/v1/catalog/queries", payload)
    second = await signed(case, "/v1/catalog/queries", payload)
    if abs(value) <= 9_007_199_254_740_991:
        assert first.status_code == 200, first.text
        assert first.json()["result"]["columns"][0]["dataType"] == "integer"
        assert first.json()["result"]["rows"] == [[value]]
        assert second.json() == first.json()
        state = "completed"
    else:
        assert first.status_code == 422, first.text
        assert first.json()["detail"]["code"] == "RESULT_INTEGER_OUT_OF_RANGE"
        assert second.status_code == 422
        assert second.json() == first.json()
        state = "failed"
    assert len(case["calls"]) == 1
    async with await psycopg.AsyncConnection.connect(case["database"]) as connection:
        rows = await (
            await connection.execute("SELECT state,failure_code FROM compiler_catalog_results_v1")
        ).fetchall()
        assert rows == [(state, None if state == "completed" else "RESULT_INTEGER_OUT_OF_RANGE")]


@pytest.mark.parametrize("route", ["query", "resume"])
@pytest.mark.parametrize("phase", ["compile", "execute"])
async def test_signed_body_disconnect_cancels_catalog_work_before_publication(
    public_case, monkeypatch, route, phase
):
    case = public_case
    payload = query_payload(case["cases"][1])
    path = "/v1/catalog/queries"
    if route == "resume":
        payload["question"] = "yield for alpha above score 1"
        initial = (await signed(case, path, payload)).json()
        path = f"/v1/catalog/clarifications/{initial['compilation']['clarification_id']}/answers"
        payload = {
            "contract_version": "catalog-answer/v1",
            "catalog": payload["catalog"],
            "revision": 1,
            "choice_id": "lab.mean_yield",
        }
    runtime_entered, runtime_release, runtime_cancelled = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    original_execute = SyntheticCatalogResolver._execute

    async def paused_execute(self, context, fragment, asset):
        runtime_entered.set()
        try:
            await runtime_release.wait()
        except asyncio.CancelledError:
            runtime_cancelled.set()
            raise
        return await original_execute(self, context, fragment, asset)

    if phase == "compile":
        case["state"]["pause"] = True
    else:
        monkeypatch.setattr(SyntheticCatalogResolver, "_execute", paused_execute)
    task, incoming, sent = await start_signed_asgi(case, path, payload)
    try:
        await asyncio.wait_for(
            case["entered"].wait() if phase == "compile" else runtime_entered.wait(), 3
        )
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 2)
        assert not case["service"]._tasks
        if phase == "compile":
            assert not case["provider"]._transport._active
            assert not runtime_entered.is_set()
        else:
            assert runtime_cancelled.is_set()
        case["release"].set()
        runtime_release.set()
        async with await psycopg.AsyncConnection.connect(case["database"]) as connection:
            assert (
                await (
                    await connection.execute(
                        "SELECT count(*) FROM compiler_catalog_results_v1 WHERE state='completed'"
                    )
                ).fetchone()
            )[0] == 0
            row = await (
                await connection.execute("SELECT payload FROM compiler_clarifications_v1")
            ).fetchone()
            saved = json.loads(row[0])
            if phase == "compile":
                assert saved["current"] is None or saved["current"]["status"] == "clarification"
        assert not any(m.get("status") == 200 for m in sent)
    finally:
        case["release"].set()
        runtime_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_disconnect_resistant_model_is_retained_but_cannot_late_commit(
    public_case, monkeypatch
):
    from semantic_backend.catalog_disconnect import _LATE_OPERATIONS

    case = public_case
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = CatalogHTTPProvider.invoke

    async def resistant(self, context, **kwargs):
        result = await original(self, context, **kwargs)
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        return result

    monkeypatch.setattr(CatalogHTTPProvider, "invoke", resistant)
    baseline = set(_LATE_OPERATIONS)
    task, incoming, sent = await start_signed_asgi(
        case, "/v1/catalog/queries", query_payload(case["cases"][1])
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 0.35)
        assert cancelled.is_set()
        retained = _LATE_OPERATIONS - baseline
        assert retained
        release.set()
        await asyncio.wait_for(asyncio.gather(*retained, return_exceptions=True), 2)
        assert not retained & _LATE_OPERATIONS
        async with await psycopg.AsyncConnection.connect(case["database"]) as connection:
            row = await (
                await connection.execute("SELECT payload FROM compiler_clarifications_v1")
            ).fetchone()
            assert json.loads(row[0])["current"] is None
            assert (
                await (
                    await connection.execute("SELECT count(*) FROM compiler_catalog_results_v1")
                ).fetchone()
            )[0] == 0
        assert not any(m.get("status") == 200 for m in sent)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*(_LATE_OPERATIONS - baseline), return_exceptions=True)


async def test_disconnect_after_committed_result_does_not_overwrite_terminal_state(
    public_case, monkeypatch
):
    case = public_case
    committed, release = asyncio.Event(), asyncio.Event()
    original = case["service"]._execute

    async def paused(*args, **kwargs):
        response = await original(*args, **kwargs)
        committed.set()
        await release.wait()
        return response

    monkeypatch.setattr(case["service"], "_execute", paused)
    payload = query_payload(case["cases"][1])
    task, incoming, _ = await start_signed_asgi(case, "/v1/catalog/queries", payload)
    try:
        await asyncio.wait_for(committed.wait(), 3)
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 2)
        async with await psycopg.AsyncConnection.connect(case["database"]) as connection:
            assert (
                await (
                    await connection.execute("SELECT state FROM compiler_catalog_results_v1")
                ).fetchone()
            )[0] == "completed"
        monkeypatch.setattr(case["service"], "_execute", original)
        replay = await signed(case, "/v1/catalog/queries", payload)
        assert replay.status_code == 200 and replay.json()["status"] == "succeeded"
        assert len(case["calls"]) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
