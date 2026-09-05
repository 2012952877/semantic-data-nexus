from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest
from query_runtime.domain import BoundSource, CapabilityCatalog, OperatorKind
from query_runtime.operators import ResourceLimits
from query_runtime.resolver import FakeResolver
from semantic_api.catalog_v1.catalog import PublishedCatalogs, pin_for
from semantic_api.catalog_v1.compiler import CatalogCompiler
from semantic_api.catalog_v1.models import CatalogCompileRequest, CatalogDocument, CompilerFailure
from semantic_api.catalog_v1.provider import CatalogHTTPProvider
from semantic_api.catalog_v1.trust import CatalogAccess
from semantic_api.catalog_v1.validator import CORE_CAPABILITIES
from semantic_api.provider_config import ProviderSettings

from semantic_backend.catalog_compilation import (
    TYPES,
    CatalogBindings,
    EntityBinding,
    FieldBinding,
    adapt_catalog,
    execute_catalog,
)

CASES = json.loads(
    (
        Path(__file__).resolve().parents[3] / "contracts" / "compiler" / "v1" / "examples.json"
    ).read_text()
)["cases"]


@asynccontextmanager
async def wire(candidate):
    requests = []
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            headers = dict(line.split(": ", 1) for line in head.decode().split("\r\n")[1:] if line)
            requests.append(json.loads(await reader.readexactly(int(headers["Content-Length"]))))
            payload = json.dumps(
                {
                    "id": "synthetic-response",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "synthetic-model",
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
            writer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/chat/completions"
    provider = CatalogHTTPProvider(
        ProviderSettings(
            mode="openai_compatible",
            model="synthetic-model",
            endpoint=endpoint,
            credential_env="SYNTHETIC_MODEL_KEY",
            allow_local_mock=True,
            max_input_tokens=65_536,
        ),
        "synthetic-test-value",
    )
    try:
        async with server:
            yield provider, requests
    finally:
        await provider.aclose()
        await asyncio.gather(*tasks)


class Authority:
    revoked = False

    def __init__(self, document):
        self.access = CatalogAccess(
            entity_ids=frozenset(e.id for e in document.entities),
            field_ids=frozenset(f.id for f in document.fields),
            metric_ids=frozenset(m.id for m in document.metrics),
            relation_ids=frozenset(r.id for r in document.relations),
            member_ids=frozenset(m.id for f in document.fields for m in f.members),
        )

    async def require(self, context, pin, permission):
        if self.revoked:
            raise CompilerFailure("MEMBERSHIP_REVOKED")
        return self.access


class NoClarification:
    async def create(self, record):
        raise AssertionError("These held-out positive questions are unambiguous")

    def lock(self, *args):
        raise AssertionError("No clarification resume in positive fixture")


class ObservedResolver(FakeResolver):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = asyncio.Event()

    async def execute(self, context, fragment, cancel_event):
        self.entered.set()
        return await super().execute(context, fragment, cancel_event)


def setup(case, provider):
    document = CatalogDocument.model_validate(case["catalog"])
    authority = Authority(document)
    now = datetime.now(UTC)
    context = SimpleNamespace(
        contract_version="trusted-context/v1",
        scope=document.scope,
        principal=SimpleNamespace(
            principal_id="synthetic-user",
            issuer="https://synthetic.invalid",
            subject="synthetic-subject",
        ),
        authentication=SimpleNamespace(
            method="oidc",
            audience="synthetic-api",
            authenticated_at=now,
            expires_at=now + timedelta(hours=1),
        ),
        membership=SimpleNamespace(
            membership_id="synthetic-membership",
            revision=1,
            authorized_at=now,
            permissions=("compiler:query",),
        ),
    )
    compiler = CatalogCompiler(
        catalogs=PublishedCatalogs((document,)),
        authorization=authority,
        provider=provider,
        clarifications=NoClarification(),
        capabilities=CORE_CAPABILITIES,
    )
    request = CatalogCompileRequest(
        contract_version="catalog-compile/v1",
        request_id="synthetic-request",
        catalog=pin_for(document),
        question=case["question"],
    )
    fields = {f.id: f for f in document.fields}
    assets = []
    tables = {}
    capabilities = {}
    for item in case["bindings"]:
        asset = EntityBinding(
            entity_id=item["entity_id"],
            source=BoundSource(
                alias=item["alias"], source_type="synthetic", object_name=item["object_name"]
            ),
            fields=tuple(
                FieldBinding(
                    field_id=concept,
                    column_name=column,
                    data_type=TYPES[fields[concept].data_type],
                )
                for concept, column in item["columns"].items()
            ),
        )
        assets.append(asset)
        rows = copy.deepcopy(case["rows"][asset.source.alias])
        for field in asset.fields:
            if fields[field.field_id].data_type == "datetime":
                for row in rows:
                    row[field.column_name] = datetime.fromisoformat(row[field.column_name])
        tables[asset.source.alias] = pa.Table.from_pylist(rows)
        capabilities[asset.source.alias] = CapabilityCatalog(
            source_alias=asset.source.alias,
            source_type="synthetic",
            operator_kinds=frozenset({OperatorKind.SELECT}),
        )
    bindings = CatalogBindings(
        contract_version="catalog-bindings/v1", catalog=pin_for(document), entities=tuple(assets)
    )
    resolver = ObservedResolver(tables=tables, catalogs=capabilities)
    return compiler, request, context, bindings, resolver, authority


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
async def test_heldout_natural_language_wire_to_typed_duckdb_result(case):
    async with wire(case["candidate"]) as (provider, requests):
        compiler, request, context, bindings, resolver, _ = setup(case, provider)
        deadline = asyncio.get_running_loop().time() + 15
        compilation = await compiler.compile(request, context=context, deadline=deadline)
        result = await execute_catalog(
            request,
            compilation,
            compiler=compiler,
            context=context,
            bindings=bindings,
            resolver=resolver,
            run_id="synthetic-" + case["name"],
            deadline=deadline,
            limits=ResourceLimits(max_rows=100, max_bytes=1_000_000),
        )
        assert result.table.to_pylist() == case["expected"]
        assert result.compilation.graph.model_dump(mode="json") == case["candidate"]["graph"]
        assert result.metadata["catalog_sha256"] == request.catalog.content_sha256
        assert result.metadata["binding_sha256"] == bindings.content_sha256
        assert result.outcome.manifest is not None
        assert result.outcome.events
        assert result.plan.nodes
        assert len(requests) == 1
        sent = requests[0]["messages"][1]["content"]
        assert case["question"] in sent
        assert all(b["object_name"] not in sent for b in case["bindings"])
        assert compilation.input_tokens == 100 and compilation.output_tokens == 200


async def test_binding_capability_pin_and_runtime_budget_fail_closed():
    case = CASES[0]
    async with wire(case["candidate"]) as (provider, _):
        compiler, request, context, bindings, resolver, _ = setup(case, provider)
        deadline = asyncio.get_running_loop().time() + 15
        compilation = await compiler.compile(request, context=context, deadline=deadline)
        _, initialized, _ = await compiler._context(request, context)
        with pytest.raises(CompilerFailure, match="SOURCE_CAPABILITY_MISSING"):
            adapt_catalog(compilation.graph, initialized, bindings, {}, run_id="synthetic")
        bad = bindings.model_copy(
            update={"catalog": bindings.catalog.model_copy(update={"revision": 9})}
        )
        with pytest.raises(CompilerFailure, match="BINDING_PIN_MISMATCH"):
            adapt_catalog(compilation.graph, initialized, bad, {}, run_id="synthetic")
        remapped = bindings.entities[0].model_copy(
            update={
                "source": bindings.entities[0].source.model_copy(
                    update={"object_name": "synthetic-rebound-asset"}
                ),
            }
        )
        bad = bindings.model_copy(update={"entities": (remapped, *bindings.entities[1:])})
        with pytest.raises(CompilerFailure, match="BINDING_PIN_MISMATCH"):
            adapt_catalog(compilation.graph, initialized, bad, {}, run_id="synthetic")
        with pytest.raises(CompilerFailure):
            await execute_catalog(
                request,
                compilation,
                compiler=compiler,
                context=context,
                bindings=bindings,
                resolver=resolver,
                run_id="synthetic-budget",
                deadline=deadline,
                limits=ResourceLimits(max_rows=1),
            )


@pytest.mark.parametrize("violation", ["key", "type", "timezone"])
async def test_source_binding_and_cardinality_conformance(violation):
    case = CASES[0]
    async with wire(case["candidate"]) as (provider, _):
        compiler, request, context, bindings, resolver, _ = setup(case, provider)
        deadline = asyncio.get_running_loop().time() + 15
        compilation = await compiler.compile(request, context=context, deadline=deadline)
        if violation == "key":
            table = resolver._tables["locations"]
            resolver._tables["locations"] = pa.concat_tables([table, table.slice(0, 1)])
            code = "JOIN_CARDINALITY_VIOLATION"
        else:
            table = resolver._tables["measurements"]
            if violation == "type":
                index = table.column_names.index("quantity")
                table = table.set_column(index, "quantity", table["quantity"].cast(pa.string()))
                code = "SOURCE_TYPE_MISMATCH"
            else:
                index = table.column_names.index("at")
                table = table.set_column(index, "at", table["at"].cast(pa.timestamp("us")))
                code = "SOURCE_TIMEZONE_REQUIRED"
            resolver._tables["measurements"] = table
        with pytest.raises(CompilerFailure, match=code):
            await execute_catalog(
                request,
                compilation,
                compiler=compiler,
                context=context,
                bindings=bindings,
                resolver=resolver,
                run_id="synthetic-" + violation,
                deadline=deadline,
                limits=ResourceLimits(max_rows=100, max_bytes=1_000_000),
            )


async def test_runtime_cancellation_and_reauthorization_before_return():
    case = CASES[1]
    async with wire(case["candidate"]) as (provider, _):
        compiler, request, context, bindings, resolver, authority = setup(case, provider)
        deadline = asyncio.get_running_loop().time() + 15
        compilation = await compiler.compile(request, context=context, deadline=deadline)
        resolver._delays = {"experiments": 1}
        task = asyncio.create_task(
            execute_catalog(
                request,
                compilation,
                compiler=compiler,
                context=context,
                bindings=bindings,
                resolver=resolver,
                run_id="synthetic-cancel",
                deadline=deadline,
                limits=ResourceLimits(),
            )
        )
        await resolver.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert resolver.active == 0
        assert resolver.cancelled
        authority.revoked = True
        with pytest.raises(CompilerFailure, match="MEMBERSHIP_REVOKED"):
            await execute_catalog(
                request,
                compilation,
                compiler=compiler,
                context=context,
                bindings=bindings,
                resolver=resolver,
                run_id="synthetic-revoked",
                deadline=deadline,
                limits=ResourceLimits(),
            )
