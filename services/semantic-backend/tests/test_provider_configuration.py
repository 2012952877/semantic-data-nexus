from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from conftest import request_for, wait_for_terminal
from semantic_api.compiler import SemanticCompiler
from semantic_api.models import SQG, CompilationMode, CompileRequest, InitializeRequest
from semantic_api.provider import StaticFixtureProvider, StructuredCompileContext, UntrustedQuestion

from semantic_backend.api import create_app
from semantic_backend.models import RunState
from semantic_backend.service import OrchestrationService

MODEL = "gpt-4o-2024-08-06"


def configure(monkeypatch, endpoint):
    monkeypatch.setenv("SEMANTIC_COMPILER_MODE", "openai_compatible")
    monkeypatch.setenv("SEMANTIC_COMPILER_ENDPOINT", endpoint)
    monkeypatch.setenv("SEMANTIC_COMPILER_MODEL", MODEL)
    monkeypatch.setenv("SEMANTIC_COMPILER_CREDENTIAL_ENV", "TEST_MODEL_KEY")
    monkeypatch.setenv("SEMANTIC_COMPILER_ALLOW_LOCAL_MOCK", "true")
    monkeypatch.setenv("TEST_MODEL_KEY", "synthetic-key")


@pytest.mark.parametrize("mode", list(CompilationMode))
async def test_default_backend_constructs_real_provider_from_environment(monkeypatch, mode):
    request = request_for("run_00000000000000000000000000000300", mode=mode)
    compiler = SemanticCompiler.from_environment({})
    init_request = InitializeRequest(
        question=request.question,
        evaluation_clock=request.evaluation_clock,
        evaluation_timezone=request.evaluation_timezone,
        compilation_mode=request.compilation_mode,
    )
    initialization = compiler.initialize(init_request, "test")
    context = StructuredCompileContext(
        question=UntrustedQuestion(value=request.question),
        compilation_mode=request.compilation_mode,
        resolved_terms=initialization.resolved_terms,
        time_windows=initialization.time_windows,
        semantic_context=initialization.selected_semantic_context,
    )
    fixture = await StaticFixtureProvider().compile(context)
    candidate = SQG.model_validate(fixture.candidate).model_dump(mode="json")
    candidate["nodes"][0]["name"] = "Mock model response, not a provider-generated fixture"
    calls = []

    async def handle(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        content_length = next(
            int(line.split(b":", 1)[1])
            for line in headers.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        )
        calls.append(json.loads(await reader.readexactly(content_length)))
        body = json.dumps(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1_750_000_000,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(candidate)},
                    }
                ],
                "usage": {"prompt_tokens": 123, "completion_tokens": 456, "total_tokens": 579},
            }
        ).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    configure(monkeypatch, f"http://127.0.0.1:{port}/v1/chat/completions")
    async with server:
        service = OrchestrationService()
        app = create_app(service)
        async with app.router.lifespan_context(app):
            assert await service.ready()
            assert not calls  # Readiness must never bill a model request.
            await service.start(request)
            status = await wait_for_terminal(service, request.run_id)
            assert status.state is RunState.SUCCEEDED
            assert status.token_usage.input_tokens == 123
            assert status.token_usage.output_tokens == 456
            artifact = await service.get_integrated_artifact(request.run_id)
            assert artifact.compile_response.candidate_sqg == candidate
            assert artifact.compile_response.token_metadata.provider_calls[0].model == MODEL
            assert len(calls) == 1
            assert calls[0]["model"] == MODEL
        assert not service.compiler.ready


async def test_backend_readiness_and_run_fail_closed_on_bad_configuration(monkeypatch):
    configure(monkeypatch, "https://private-not-allowlisted.invalid/v1/chat/completions")
    service = OrchestrationService()
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 503
        request = request_for("run_00000000000000000000000000000301")
        await service.start(request)
        status = await wait_for_terminal(service, request.run_id)
        assert status.state is RunState.FAILED
        detail = await service.get_detail(request.run_id)
        assert detail.result is None and detail.manifest is None
        assert any(item.code == "PROVIDER_CONFIGURATION" for item in status.diagnostics)


async def test_backend_cancel_closes_real_provider_socket(monkeypatch):
    entered = asyncio.Event()
    disconnected = asyncio.Event()

    async def stall(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        entered.set()
        await reader.read()  # Include request bytes, then observe connection EOF.
        disconnected.set()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(stall, "127.0.0.1", 0)
    configure(
        monkeypatch,
        f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/chat/completions",
    )
    async with server:
        service = OrchestrationService()
        try:
            request = request_for("run_00000000000000000000000000000302")
            await service.start(request)
            await asyncio.wait_for(entered.wait(), 2)
            status = await service.cancel(request.run_id)
            await asyncio.wait_for(disconnected.wait(), 2)
            assert status.state is RunState.CANCELLED
            assert (await service.get_detail(request.run_id)).manifest is None
        finally:
            await service.shutdown()


async def test_static_default_is_explicit_and_offline():
    compiler = SemanticCompiler.from_environment({})
    request = request_for("run_00000000000000000000000000000303")
    response = await compiler.compile(
        CompileRequest(
            question=request.question,
            evaluation_clock=request.evaluation_clock,
            evaluation_timezone=request.evaluation_timezone,
        ),
        "test",
    )
    assert response.normalized_sqg is not None
    assert response.token_metadata.provider_calls == []
    await compiler.aclose()
