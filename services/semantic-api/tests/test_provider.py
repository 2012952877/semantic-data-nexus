from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.identity.aio import (
    AzureCliCredential,
    ManagedIdentityCredential,
    WorkloadIdentityCredential,
)
from httpcore._backends.auto import AutoBackend
from test_structured_provider import AZURE_TOKEN, SUBSCRIPTION, TENANT

import semantic_api.azure_credentials as credentials
from semantic_api.azure_credentials import (
    AI_SCOPE,
    AzureCredentialConfig,
    AzureCredentialError,
    create_azure_credential,
)
from semantic_api.provider import (
    ProviderInvoker,
    ProviderResult,
    ProviderTimeoutError,
    StaticFixtureProvider,
)


@pytest.mark.parametrize("timeout", [0, -1, math.inf, math.nan])
def test_provider_timeout_must_be_finite_and_positive(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        ProviderInvoker(StaticFixtureProvider(), timeout_seconds=timeout)


async def test_provider_timeout_cancels_underlying_call(compile_context: object) -> None:
    provider = StaticFixtureProvider(delay_seconds=1)
    invoker = ProviderInvoker(provider, timeout_seconds=0.01)

    with pytest.raises(ProviderTimeoutError):
        await invoker.compile(compile_context)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert provider.cancelled is True


async def test_caller_cancellation_propagates(compile_context: object) -> None:
    provider = StaticFixtureProvider(delay_seconds=1)
    invoker = ProviderInvoker(provider, timeout_seconds=2)
    task = asyncio.create_task(invoker.compile(compile_context))  # type: ignore[arg-type]
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled is True


async def test_cancellation_suppressing_late_result_is_rejected(
    compile_context: object,
) -> None:
    class CancellationSuppressingProvider(StaticFixtureProvider):
        async def compile(self, context: object) -> ProviderResult:  # type: ignore[override]
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                return ProviderResult(candidate=self._quarterly_profit([]))
            raise AssertionError("provider unexpectedly completed before deadline")

    invoker = ProviderInvoker(CancellationSuppressingProvider(), timeout_seconds=0.01)

    with pytest.raises(ProviderTimeoutError):
        await invoker.compile(compile_context)  # type: ignore[arg-type]
    await asyncio.sleep(0)


async def test_static_provider_is_deterministic(compile_context: object) -> None:
    provider = StaticFixtureProvider()

    first = await provider.compile(compile_context)  # type: ignore[arg-type]
    second = await provider.compile(compile_context)  # type: ignore[arg-type]

    assert first.candidate == second.candidate
    assert first.candidate["schema_version"] == "sqg.v0"


def identity_config(mode="azure_cli", **overrides):
    return AzureCredentialConfig.model_validate(
        {
            "mode": mode,
            "tenant_id": TENANT,
            "subscription_id": SUBSCRIPTION if mode == "azure_cli" else "",
            **overrides,
        }
    )


def clear_identity_environment(monkeypatch):
    for name in credentials._ENV_SELECTORS:
        monkeypatch.delenv(name, raising=False)


def offline_cli(monkeypatch, script):
    """Isolate process-lifetime tests; real batch dispatch is covered separately below."""
    original = asyncio.create_subprocess_exec
    calls, processes = [], []
    monkeypatch.setattr(credentials.shutil, "which", lambda _: sys.executable)
    monkeypatch.setattr(credentials, "_windows_cli_command", lambda args: args)

    async def create(*args, **kwargs):
        if len(args) > 1 and args[1] == "account":
            calls.append(args)
            process = await original(sys.executable, "-u", "-c", script, *args[1:], **kwargs)
            processes.append(process)
            return process
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    return calls, processes


async def test_owned_cli_explicit_subscription_and_routing(monkeypatch):
    response = json.dumps({"accessToken": AZURE_TOKEN, "expires_on": int(time.time()) + 3600})
    calls, processes = offline_cli(monkeypatch, f"print({response!r})")
    credential = create_azure_credential(identity_config())
    try:
        token = await credential.get_token(AI_SCOPE)
        assert token.token == AZURE_TOKEN
        assert calls[0][1:] == (
            "account",
            "get-access-token",
            "--output",
            "json",
            "--resource",
            "https://ai.azure.com",
            "--subscription",
            SUBSCRIPTION,
        )
        assert "--tenant" not in calls[0]
        assert processes[0].returncode == 0
        assert (await credential.get_token(AI_SCOPE)).token == token.token
        assert len(calls) == 1
        with pytest.raises(AzureCredentialError):
            await credential.get_token("https://unapproved.invalid/.default")
    finally:
        await credential.close()


def windows_process_image(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    assert handle
    try:
        buffer = ctypes.create_unicode_buffer(32_768)
        size = ctypes.c_uint32(len(buffer))
        assert kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
        return buffer.value
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="Real Windows batch dispatch regression")
@pytest.mark.parametrize("folder", ["CLI with spaces (x86)", "CLI(no-spaces)"])
@pytest.mark.parametrize("action", ["success", "timeout", "cancel", "close"])
async def test_real_cmd_dispatch_ignores_parent_comspec(monkeypatch, tmp_path, folder, action):
    directory = tmp_path / folder
    directory.mkdir()
    batch, program, observation = (
        directory / "az.cmd",
        directory / "synthetic.py",
        directory / "arguments.json",
    )
    connected = asyncio.get_running_loop().create_future()
    server = await asyncio.start_server(
        lambda reader, writer: connected.set_result((reader, writer)), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    response = json.dumps({"accessToken": AZURE_TOKEN, "expires_on": int(time.time()) + 3600})
    program.write_text(
        "import json,pathlib,socket,sys,time\n"
        f"pathlib.Path({str(observation)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"s=socket.create_connection(('127.0.0.1',{port}))\ns.sendall(b'1')\n"
        + (f"print({response!r})\n" if action == "success" else "time.sleep(60)\n"),
        encoding="utf-8",
    )
    batch.write_text(f'@echo off\n"{sys.executable}" "{program}" %*\n', encoding="utf-8")
    monkeypatch.setenv("COMSPEC", sys.executable)
    monkeypatch.setattr(credentials.shutil, "which", lambda _: str(batch))
    original_spawn = asyncio.create_subprocess_exec
    calls, images, processes = [], [], []

    async def inspect_spawn(*args, **kwargs):
        assert kwargs.get("shell", False) is False
        assert kwargs["creationflags"] == 4
        process = await original_spawn(*args, **kwargs)
        # Inspect the actual suspended process, not just a mocked argument list.
        images.append(windows_process_image(process.pid))
        calls.append((args, kwargs))
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", inspect_spawn)
    credential = create_azure_credential(identity_config(timeout_seconds=30))
    deadline = asyncio.timeout(30)

    async def invoke():
        async with deadline:
            return await credential.get_token(AI_SCOPE)

    async with server:
        task = asyncio.create_task(invoke())
        writer = None
        try:
            reader, writer = await asyncio.wait_for(connected, 20)
            assert await asyncio.wait_for(reader.readexactly(1), 1) == b"1"
            args, options = calls[0]
            assert Path(images[0]).name.lower() == "cmd.exe"
            assert os.path.normcase(images[0]) == os.path.normcase(args[0])
            assert args[1:4] == ("/d", "/v:off", "/c")
            resolved = str(batch.resolve())
            assert args[4] == (
                resolved if " " in resolved else resolved.replace("(", "^(").replace(")", "^)")
            )
            assert options["env"]["COMSPEC"] == args[0]
            assert os.environ["COMSPEC"] == sys.executable
            assert options["cwd"] == str(Path(args[0]).parent)
            assert json.loads(observation.read_text()) == [
                "account",
                "get-access-token",
                "--output",
                "json",
                "--resource",
                "https://ai.azure.com",
                "--subscription",
                SUBSCRIPTION,
            ]
            if action == "success":
                assert (await task).token == AZURE_TOKEN
            elif action == "timeout":
                deadline.reschedule(asyncio.get_running_loop().time() + 0.05)
                with pytest.raises(TimeoutError):
                    await task
            else:
                if action == "cancel":
                    task.cancel()
                else:
                    await credential.close()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert processes[0].returncode is not None
            try:
                assert await asyncio.wait_for(reader.read(), 1) == b""
            except ConnectionResetError:
                pass
        finally:
            await credential.close()
            await asyncio.gather(task, return_exceptions=True)
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionResetError:
                    pass


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command path validation")
@pytest.mark.parametrize(
    "path",
    [
        "az.cmd",
        r"C:az.cmd",
        r"\\server\share\az.cmd",
        r"C:\synthetic%EXPANSION%\az.cmd",
        r"C:\synthetic&command\az.cmd",
        r"C:\synthetic!expansion!\az.cmd",
        "C:\\synthetic\ncommand\\az.cmd",
    ],
)
def test_batch_command_rejects_untrusted_paths_before_spawn(path):
    with pytest.raises(AzureCredentialError) as caught:
        credentials._windows_cli_command([path, "account"])
    assert caught.value.code == "AUTH_UNAVAILABLE"


@pytest.mark.skipif(sys.platform != "win32", reason="OS-owned interpreter location")
def test_batch_interpreter_does_not_use_systemroot_or_comspec(monkeypatch, tmp_path):
    batch = tmp_path / "az.cmd"
    batch.write_text("@exit /b 0\n")
    expected = credentials._windows_cli_command([str(batch), "account"])[0]
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setenv("COMSPEC", sys.executable)
    assert credentials._windows_cli_command([str(batch), "account"])[0] == expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows SDK selector-loop regression")
def test_actual_sdk_selector_fallback_loses_account_but_owned_factory_refuses(monkeypatch):
    import azure.identity.aio._credentials.azure_cli as sdk_cli

    sync_config = []

    class UnsafeSyncDouble:
        def __init__(self, **kwargs):
            sync_config.append(kwargs)

        def get_token(self, *args, **kwargs):
            return AccessToken(AZURE_TOKEN, int(time.time()) + 3600)

    monkeypatch.setattr(sdk_cli, "_SyncAzureCliCredential", UnsafeSyncDouble)
    loop = asyncio.SelectorEventLoop()

    async def exercise():
        sdk = AzureCliCredential(subscription=SUBSCRIPTION, process_timeout=1)
        await sdk.get_token(AI_SCOPE)
        assert sync_config == [{}]  # Real installed SDK's defective fallback.
        bounded = create_azure_credential(identity_config())
        with pytest.raises(AzureCredentialError) as caught:
            await bounded.get_token(AI_SCOPE)
        assert caught.value.code == "AUTH_UNAVAILABLE"
        assert sync_config == [{}]  # Never entered that fallback again.
        await bounded.close()
        await sdk.close()

    try:
        loop.run_until_complete(exercise())
    finally:
        loop.close()


async def test_actual_sdk_cli_cancellation_does_not_terminate(monkeypatch):
    import azure.identity.aio._credentials.azure_cli as sdk_cli

    class ProcessDouble:
        def __init__(self):
            self.entered = asyncio.Event()
            self.kills = 0

        async def communicate(self):
            self.entered.set()
            await asyncio.Event().wait()

        def kill(self):
            self.kills += 1

    process = ProcessDouble()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(sdk_cli.shutil, "which", lambda _: "synthetic-cli")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(sdk_cli._run_command(["account", "get-access-token"], 10))
    await process.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.kills == 0  # Reproduce the installed SDK, without a real orphan process.


@pytest.mark.parametrize("action", ["timeout", "cancel", "close", "orphan"])
async def test_owned_cli_terminates_and_reaps_actual_process_tree(monkeypatch, action):
    connected = asyncio.get_running_loop().create_future()
    server = await asyncio.start_server(
        lambda reader, writer: connected.set_result((reader, writer)), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    child = (
        "import socket,time;"
        f"s=socket.create_connection(('127.0.0.1',{port}));"
        "s.sendall(b'1');time.sleep(60)"
    )
    script = f"import subprocess,sys,time;p=subprocess.Popen([sys.executable,'-c',{child!r}]);" + (
        "sys.exit(0)" if action == "orphan" else "time.sleep(60)"
    )
    _, processes = offline_cli(monkeypatch, script)
    credential = create_azure_credential(identity_config(timeout_seconds=30))
    outer_deadline = asyncio.timeout(30)

    async def invoke():
        async with outer_deadline:
            return await credential.get_token(AI_SCOPE)

    async with server:
        task = asyncio.create_task(invoke())
        writer = None
        try:
            reader, writer = await asyncio.wait_for(connected, 20)
            assert await asyncio.wait_for(reader.readexactly(1), 1) == b"1"
            if action == "cancel":
                task.cancel()
            elif action == "close":
                await credential.close()
            if action in ("timeout", "orphan"):
                outer_deadline.reschedule(asyncio.get_running_loop().time() + 0.05)
                with pytest.raises(TimeoutError):
                    await task
            else:
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert processes[0].returncode is not None
            try:
                assert await asyncio.wait_for(reader.read(), 1) == b""
            except ConnectionResetError:
                pass  # Windows aborts the descendant socket on process termination.
            assert not credential._active
        finally:
            await credential.close()
            await asyncio.gather(task, return_exceptions=True)
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionResetError:
                    pass


async def test_owned_cli_internal_deadline_terminates_started_process(monkeypatch):
    _, processes = offline_cli(monkeypatch, "import time;time.sleep(60)")
    credential = create_azure_credential(identity_config(timeout_seconds=0.1))
    try:
        with pytest.raises(AzureCredentialError) as caught:
            await credential.get_token(AI_SCOPE)
        assert caught.value.code == "TIMEOUT"
        assert processes and processes[0].returncode is not None
        assert not credential._active
    finally:
        await credential.close()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_owned_cli_output_is_bounded_before_json_parse(monkeypatch, stream):
    _, processes = offline_cli(
        monkeypatch,
        f"import sys,time;sys.{stream}.write('x'*2000000);sys.{stream}.flush();time.sleep(60)",
    )
    credential = create_azure_credential(identity_config(max_response_bytes=1024))
    try:
        with pytest.raises(AzureCredentialError) as caught:
            await credential.get_token(AI_SCOPE)
        assert caught.value.code == "AUTH_LIMIT"
        assert processes[0].returncode is not None
        assert not credential._active
    finally:
        await credential.close()


async def test_actual_sdk_selects_ambient_workload_but_factory_rejects(monkeypatch, tmp_path):
    clear_identity_environment(monkeypatch)
    monkeypatch.setenv("AZURE_TENANT_ID", TENANT)
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com")
    monkeypatch.setenv("AZURE_CLIENT_ID", SUBSCRIPTION)
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(tmp_path / "must-not-read"))
    sdk = ManagedIdentityCredential()
    assert isinstance(sdk._credential, WorkloadIdentityCredential)
    await sdk.close()
    bounded = create_azure_credential(identity_config("managed_identity"))
    with pytest.raises(AzureCredentialError) as caught:
        await bounded.get_token(AI_SCOPE)
    assert caught.value.code == "AUTH_UNAVAILABLE"
    assert bounded._sdk is None
    await bounded.close()


async def test_workload_mode_unavailable_before_any_sdk_file_read(monkeypatch, tmp_path):
    import builtins

    assertion = tmp_path / "synthetic-large-assertion"
    assertion.write_bytes(b"x" * (128 * 1024))
    sdk = WorkloadIdentityCredential(
        tenant_id=TENANT, client_id=SUBSCRIPTION, token_file_path=str(assertion)
    )
    # The real SDK does an unrestricted synchronous read, including the entire assertion.
    assert len(sdk._get_service_account_token()) == 128 * 1024
    await sdk.close()
    original_open = builtins.open
    reads = []

    def observe_open(file, *args, **kwargs):
        if str(file) == str(assertion):
            reads.append(file)
            raise AssertionError("Unavailable owned workload mode must not open the assertion")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", observe_open)
    credential = create_azure_credential(
        identity_config("workload_identity", client_id=SUBSCRIPTION, token_file=str(assertion))
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(AzureCredentialError) as caught:
        await credential.get_token(AI_SCOPE)
    assert caught.value.code == "AUTH_UNAVAILABLE"
    assert asyncio.get_running_loop().time() - started < 0.1
    assert reads == [] and credential._sdk is None
    await credential.close()


@asynccontextmanager
async def identity_server(monkeypatch, body, *, status=200, encoding=None, stall=False):
    calls, tasks = [], set()
    entered, disconnected = asyncio.Event(), asyncio.Event()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            calls.append(header.split(b"\r\n", 1)[0].decode())
            entered.set()
            writer.write(
                f"HTTP/1.1 {status} Synthetic\r\nContent-Type: application/json\r\n".encode()
                + (f"Content-Encoding: {encoding}\r\n".encode() if encoding else b"")
                + b"Transfer-Encoding: chunked\r\n\r\n"
            )
            await writer.drain()
            if stall:
                await reader.read()
                disconnected.set()
                return
            for offset in range(0, len(body), 4096):
                chunk = body[offset : offset + 4096]
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            disconnected.set()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                disconnected.set()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    original_client = httpx.AsyncClient

    class LoopbackBackend(AutoBackend):
        async def connect_tcp(self, host, port, **kwargs):
            assert host in ("169.254.169.254", "127.0.0.1")
            return await super().connect_tcp(
                "127.0.0.1", server.sockets[0].getsockname()[1], **kwargs
            )

    def client(**kwargs):
        transport = httpx.AsyncHTTPTransport(retries=0)
        transport._pool._network_backend = LoopbackBackend()
        return original_client(**kwargs, transport=transport)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    try:
        async with server:
            yield calls, entered, disconnected, port
    finally:
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def identity_response(**extra):
    return json.dumps(
        {
            "access_token": AZURE_TOKEN,
            "expires_in": 3600,
            "expires_on": int(time.time()) + 3600,
            "resource": "https://ai.azure.com",
            "token_type": "Bearer",
            **extra,
        }
    ).encode()


@pytest.mark.parametrize("source", ["imds", "app_service"])
async def test_actual_sdk_managed_identity_uses_bounded_transport(monkeypatch, source):
    clear_identity_environment(monkeypatch)
    async with identity_server(monkeypatch, identity_response()) as (calls, _, _, port):
        endpoint = f"http://127.0.0.1:{port}/msi/token" if source == "app_service" else ""
        if source == "app_service":
            monkeypatch.setenv("IDENTITY_ENDPOINT", endpoint)
            monkeypatch.setenv("IDENTITY_HEADER", "synthetic-identity-header")
        credential = create_azure_credential(
            identity_config(
                "managed_identity",
                managed_identity_source=source,
                managed_identity_endpoint=endpoint,
            )
        )
        try:
            result = await credential.get_token(AI_SCOPE)
            assert result.token == AZURE_TOKEN
            assert isinstance(credential._sdk, ManagedIdentityCredential)
            assert len(calls) == 1
            assert "resource=https://ai.azure.com" in calls[0]
            # Even a cached token cannot conceal a changed identity selector.
            monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "unapproved-file")
            with pytest.raises(AzureCredentialError) as caught:
                await credential.get_token(AI_SCOPE)
            assert caught.value.code == "AUTH_UNAVAILABLE"
            assert len(calls) == 1
        finally:
            await credential.close()


@pytest.mark.parametrize(
    "status,encoding", [(200, None), (400, None), (200, "gzip"), (400, "gzip")]
)
async def test_sdk_identity_body_limit_before_json_or_decompression(monkeypatch, status, encoding):
    import gzip

    clear_identity_environment(monkeypatch)
    body = identity_response(padding="x" * (2 * 1024 * 1024))
    if encoding:
        body = gzip.compress(body)
    async with identity_server(monkeypatch, body, status=status, encoding=encoding) as (
        calls,
        _,
        _,
        _,
    ):
        credential = create_azure_credential(identity_config("managed_identity"))
        try:
            with pytest.raises(AzureCredentialError) as caught:
                await credential.get_token(AI_SCOPE)
            assert caught.value.code == "AUTH_LIMIT"
            assert len(calls) == 1 and credential._cached is None
        finally:
            await credential.close()


async def test_sdk_identity_body_deadline_disconnects_before_return(monkeypatch):
    clear_identity_environment(monkeypatch)
    async with identity_server(monkeypatch, b"", stall=True) as (_, entered, disconnected, _):
        credential = create_azure_credential(identity_config("managed_identity", timeout_seconds=1))
        task = asyncio.create_task(credential.get_token(AI_SCOPE))
        with pytest.raises(AzureCredentialError) as caught:
            await task
        assert caught.value.code == "TIMEOUT"
        assert entered.is_set()
        await asyncio.wait_for(disconnected.wait(), 1)
        await credential.close()


async def test_actual_sdk_default_transport_accepts_oversize_but_owned_transport_refuses(
    monkeypatch,
):
    clear_identity_environment(monkeypatch)
    body = identity_response(padding="x" * (2 * 1024 * 1024))
    async with identity_server(monkeypatch, body) as (calls, _, _, port):
        endpoint = f"http://127.0.0.1:{port}/msi/token"
        monkeypatch.setenv("IDENTITY_ENDPOINT", endpoint)
        monkeypatch.setenv("IDENTITY_HEADER", "synthetic-identity-header")
        sdk = ManagedIdentityCredential()
        try:
            assert (await sdk.get_token(AI_SCOPE)).token == AZURE_TOKEN
        finally:
            await sdk.close()
        bounded = create_azure_credential(
            identity_config(
                "managed_identity",
                managed_identity_source="app_service",
                managed_identity_endpoint=endpoint,
            )
        )
        try:
            with pytest.raises(AzureCredentialError) as caught:
                await bounded.get_token(AI_SCOPE)
            assert caught.value.code == "AUTH_LIMIT"
            assert len(calls) == 2
        finally:
            await bounded.close()


@pytest.mark.parametrize(
    "selector",
    [
        "IDENTITY_ENDPOINT",
        "IDENTITY_SERVER_THUMBPRINT",
        "IMDS_ENDPOINT",
        "MSI_ENDPOINT",
        "MSI_SECRET",
        "AZURE_POD_IDENTITY_AUTHORITY_HOST",
    ],
)
async def test_managed_identity_cannot_switch_ambient_source(monkeypatch, selector):
    clear_identity_environment(monkeypatch)
    monkeypatch.setenv(selector, "synthetic-unapproved-selector")
    bounded = create_azure_credential(identity_config("managed_identity"))
    with pytest.raises(AzureCredentialError) as caught:
        await bounded.get_token(AI_SCOPE)
    assert caught.value.code == "AUTH_UNAVAILABLE"
    assert bounded._sdk is None
    await bounded.close()
