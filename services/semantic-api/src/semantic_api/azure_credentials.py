"""Owned, bounded Azure credentials. No ambient identity chain or caller-chosen authority."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import logging
import ntpath
import os
import re
import shutil
import signal
import sys
import time
from collections.abc import Awaitable
from contextvars import ContextVar
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, Final, Literal, Self
from urllib.parse import parse_qs, urlsplit

import httpx
from azure.core.credentials import AccessToken
from azure.core.exceptions import (
    ClientAuthenticationError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.core.pipeline.transport import AsyncHttpResponse, AsyncHttpTransport, HttpRequest
from azure.identity.aio import ManagedIdentityCredential
from pydantic import BaseModel, ConfigDict, Field, model_validator

AI_SCOPE: Final = "https://ai.azure.com/.default"
POSTGRES_SCOPE: Final = "https://ossrdbms-aad.database.windows.net/.default"
STORAGE_SCOPE: Final = "https://storage.azure.com/.default"
type AzureTokenScope = Literal[
    "https://ai.azure.com/.default",
    "https://ossrdbms-aad.database.windows.net/.default",
    "https://storage.azure.com/.default",
]

_TOKEN_AUDIENCES = MappingProxyType(
    {
        AI_SCOPE: ("https://ai.azure.com", "https://ai.azure.com/"),
        POSTGRES_SCOPE: ("https://ossrdbms-aad.database.windows.net",),
        STORAGE_SCOPE: ("https://storage.azure.com", "https://storage.azure.com/"),
    }
)
CREDENTIAL_POLICY = "bounded-azure-identity/v3"
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_IMDS = "http://169.254.169.254/metadata/identity/oauth2/token"
_ENV_SELECTORS = (
    "AZURE_FEDERATED_TOKEN_FILE",
    "AZURE_POD_IDENTITY_AUTHORITY_HOST",
    "IDENTITY_ENDPOINT",
    "IDENTITY_HEADER",
    "IDENTITY_SERVER_THUMBPRINT",
    "IMDS_ENDPOINT",
    "MSI_ENDPOINT",
    "MSI_SECRET",
)
_private_identity: ContextVar[bool] = ContextVar("bounded_private_identity", default=False)


class AzureCredentialError(ClientAuthenticationError):
    def __init__(self, code: str) -> None:
        super().__init__(message="The configured Azure credential could not complete.")
        self.code = code


class AzureCredentialConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    mode: Literal["azure_cli", "managed_identity", "workload_identity"]
    tenant_id: str = Field(pattern=_UUID)
    subscription_id: str = Field(default="", pattern=f"^$|{_UUID}")
    client_id: str = Field(default="", pattern=f"^$|{_UUID}")
    token_file: str = Field(default="", max_length=4096, repr=False)
    scope: AzureTokenScope = AI_SCOPE
    managed_identity_source: Literal["imds", "app_service"] = "imds"
    managed_identity_endpoint: str = Field(default="", max_length=256)
    timeout_seconds: float = Field(default=5, gt=0, le=120, allow_inf_nan=False)
    max_response_bytes: int = Field(default=65_536, ge=1024, le=65_536)

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if self.mode == "azure_cli":
            if not self.subscription_id or self.client_id or self.token_file:
                raise ValueError("Explicit CLI subscription is required")
        elif self.subscription_id:
            raise ValueError("Subscription is only valid for CLI credentials")
        if self.mode != "managed_identity" and (
            self.managed_identity_source != "imds" or self.managed_identity_endpoint
        ):
            raise ValueError("Managed identity configuration is not applicable")
        if self.mode == "managed_identity":
            if self.token_file:
                raise ValueError("Managed identity cannot read workload assertions")
            if self.managed_identity_source == "imds":
                if self.managed_identity_endpoint:
                    raise ValueError("IMDS has a fixed endpoint")
            else:
                url = urlsplit(self.managed_identity_endpoint)
                if not (
                    url.scheme == "http"
                    and url.hostname in ("127.0.0.1", "localhost")
                    and url.port is not None
                    and 1 <= url.port <= 65_535
                    and url.netloc == f"{url.hostname}:{url.port}"
                    and url.path == "/msi/token"
                    and not url.query
                    and not url.fragment
                ):
                    raise ValueError("An exact loopback App Service endpoint is required")
        return self

    @property
    def identity_endpoint(self) -> str:
        return _IMDS if self.managed_identity_source == "imds" else self.managed_identity_endpoint


class _IdentityLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _private_identity.get()


def _filter_logs() -> None:
    for name in list(logging.Logger.manager.loggerDict):
        if name.startswith(("azure.", "msal.", "httpcore.")) or name == "httpx":
            logger = logging.getLogger(name)
            if not any(isinstance(item, _IdentityLogFilter) for item in logger.filters):
                logger.addFilter(_IdentityLogFilter())


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _json(data: bytes) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError):
        raise AzureCredentialError("AUTH") from None


def validate_token_routing(token: AccessToken, config: AzureCredentialConfig) -> None:
    value = token.token
    if (
        not value
        or len(value) > 16_384
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
        or token.expires_on <= time.time()
    ):
        raise AzureCredentialError("AUTH")
    try:
        parts = value.split(".")
        if len(parts) != 3:
            raise ValueError("Token shape")
        claims = _json(
            base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True)
        )
    except ValueError:
        raise AzureCredentialError("AUTH") from None
    # A routing guard only. Azure services remain responsible for signature and RBAC.
    if (
        not isinstance(claims, dict)
        or claims.get("tid") != config.tenant_id
        or claims.get("aud") not in _TOKEN_AUDIENCES[config.scope]
    ):
        raise AzureCredentialError("AUTH")


class _IdentityResponse(AsyncHttpResponse):
    def __init__(self, request: HttpRequest, response: httpx.Response, body: bytes) -> None:
        super().__init__(request, None)
        self.status_code = response.status_code
        self.headers = dict(response.headers)
        self.reason = response.reason_phrase
        self.content_type = response.headers.get("content-type")
        self._body = body

    def body(self) -> bytes:
        return self._body


class _IdentityTransport(AsyncHttpTransport[HttpRequest, AsyncHttpResponse]):
    def __init__(self, config: AzureCredentialConfig) -> None:
        self.config = config
        self._closed = False
        self.failure: AzureCredentialError | None = None

    async def open(self) -> None:
        if self._closed:
            raise AzureCredentialError("AUTH_UNAVAILABLE")

    async def close(self) -> None:
        self._closed = True

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def send(self, request: HttpRequest, **kwargs: Any) -> AsyncHttpResponse:
        self.failure = None
        try:
            return await self._send(request)
        except AzureCredentialError as error:
            self.failure = error
            raise

    async def _send(self, request: HttpRequest) -> AsyncHttpResponse:
        await self.open()
        url = urlsplit(request.url)
        endpoint = urlsplit(self.config.identity_endpoint)
        query = parse_qs(url.query, keep_blank_values=True)
        expected_resource = self.config.scope.removesuffix("/.default")
        if (
            request.method != "GET"
            or (url.scheme, url.netloc, url.path)
            != (endpoint.scheme, endpoint.netloc, endpoint.path)
            or url.fragment
            or set(query) - {"api-version", "resource", "client_id"}
            or query.get("resource") != [expected_resource]
            or query.get("client_id", [])
            != ([self.config.client_id] if self.config.client_id else [])
            or len(query.get("api-version", [])) != 1
        ):
            raise AzureCredentialError("AUTH")
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=self.config.timeout_seconds,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        ) as client:
            async with client.stream(
                request.method,
                request.url,
                headers={**request.headers, "Accept-Encoding": "identity"},
            ) as response:
                # Reject compressed identity responses before any decompression.
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise AzureCredentialError("AUTH_LIMIT")
                if 300 <= response.status_code < 400:
                    raise AzureCredentialError("AUTH")
                length = response.headers.get("content-length")
                if length is not None and (
                    not length.isascii()
                    or not length.isdecimal()
                    or len(length) > 8
                    or int(length) > self.config.max_response_bytes
                ):
                    raise AzureCredentialError("AUTH_LIMIT")
                body = bytearray()
                async for chunk in response.aiter_raw(chunk_size=8192):
                    if len(body) + len(chunk) > self.config.max_response_bytes:
                        raise AzureCredentialError("AUTH_LIMIT")
                    body.extend(chunk)
                return _IdentityResponse(request, response, bytes(body))


async def _settle(operation: Awaitable[None]) -> None:
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


class _JobLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("minimum", ctypes.c_size_t),
        ("maximum", ctypes.c_size_t),
        ("active_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority", ctypes.c_uint32),
        ("scheduling", ctypes.c_uint32),
        ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("usage", ctypes.c_uint32),
        ("thread_id", ctypes.c_uint32),
        ("process_id", ctypes.c_uint32),
        ("base_priority", ctypes.c_int32),
        ("delta_priority", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
    ]


class _WindowsJob:
    """Assign the suspended CLI before it can spawn children; never rely on parent PID lifetime."""

    def __init__(self) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel = kernel
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p),
            (
                "SetInformationJobObject",
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32],
                ctypes.c_int,
            ),
            ("AssignProcessToJobObject", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
            ("OpenProcess", [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p),
            ("CreateToolhelp32Snapshot", [ctypes.c_uint32, ctypes.c_uint32], ctypes.c_void_p),
            ("Thread32First", [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry)], ctypes.c_int),
            ("Thread32Next", [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry)], ctypes.c_int),
            ("OpenThread", [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p),
            ("ResumeThread", [ctypes.c_void_p], ctypes.c_uint32),
            ("CloseHandle", [ctypes.c_void_p], ctypes.c_int),
            ("TerminateJobObject", [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
        ):
            function = getattr(kernel, name)
            function.argtypes = args
            function.restype = result
        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise AzureCredentialError("AUTH_UNAVAILABLE")
        limits = _JobLimits()
        limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; breakaway is not allowed.
        if not kernel.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            kernel.CloseHandle(self._handle)
            self._handle = None
            raise AzureCredentialError("AUTH_UNAVAILABLE")

    def attach_and_resume(self, pid: int) -> None:
        kernel = self._kernel
        process = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise AzureCredentialError("AUTH_UNAVAILABLE")
        try:
            if not kernel.AssignProcessToJobObject(self._handle, process):
                raise AzureCredentialError("AUTH_UNAVAILABLE")
        finally:
            kernel.CloseHandle(process)
        snapshot = kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise AzureCredentialError("AUTH_UNAVAILABLE")
        try:
            entry = _ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.process_id == pid:
                    thread = kernel.OpenThread(2, False, entry.thread_id)
                    if not thread:
                        raise AzureCredentialError("AUTH_UNAVAILABLE")
                    try:
                        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise AzureCredentialError("AUTH_UNAVAILABLE")
                        return
                    finally:
                        kernel.CloseHandle(thread)
                found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise AzureCredentialError("AUTH_UNAVAILABLE")
        finally:
            kernel.CloseHandle(snapshot)

    def close(self) -> None:
        if self._handle:
            try:
                if not self._kernel.TerminateJobObject(self._handle, 1):
                    raise AzureCredentialError("AUTH_CLEANUP_FAILED")
            finally:
                self._kernel.CloseHandle(self._handle)
                self._handle = None


async def _terminate_cli(
    process: asyncio.subprocess.Process,
    job: _WindowsJob | None,
) -> None:
    if job is not None:
        job.close()
        # Also covers a suspended process whose job assignment failed.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    elif sys.platform != "win32":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # A settled process group has no remaining descendants.
    else:
        raise AzureCredentialError("AUTH_CLEANUP_FAILED")


async def _read_pipe(reader: asyncio.StreamReader, maximum: int) -> bytes:
    body = bytearray()
    while chunk := await reader.read(8192):
        if len(body) + len(chunk) > maximum:
            raise AzureCredentialError("AUTH_LIMIT")
        body.extend(chunk)
    return bytes(body)


def _cli_supported() -> bool:
    return sys.platform != "win32" or isinstance(
        asyncio.get_running_loop(), asyncio.ProactorEventLoop
    )


def _windows_cli_command(arguments: list[str]) -> list[str]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    directory = kernel.GetSystemDirectoryW
    directory.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    directory.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32_768)
    size = directory(buffer, len(buffer))
    if size == 0 or size >= len(buffer):
        raise AzureCredentialError("AUTH_UNAVAILABLE")
    interpreter = ntpath.join(buffer.value, "cmd.exe")
    executable = arguments[0]
    drive, tail = ntpath.splitdrive(executable)
    if (
        not re.fullmatch(r"[A-Za-z]:", drive)
        or not tail.startswith("\\")
        or len(executable) > 4096
        or any(c in '&|<>^%!"' or ord(c) < 32 for c in executable)
        or any(not re.fullmatch(r"[A-Za-z0-9:/._-]+", arg) for arg in arguments[1:])
    ):
        raise AzureCredentialError("AUTH_UNAVAILABLE")
    try:
        cli_path = Path(executable).resolve(strict=True)
    except (OSError, RuntimeError):
        raise AzureCredentialError("AUTH_UNAVAILABLE") from None
    resolved = str(cli_path)
    if (
        cli_path.name.lower() != "az.cmd"
        or len(resolved) > 4096
        or not cli_path.is_file()
        or not re.fullmatch(r"[A-Za-z]:", ntpath.splitdrive(resolved)[0])
        or any(c in '&|<>^%!"' or ord(c) < 32 for c in resolved)
    ):
        raise AzureCredentialError("AUTH_UNAVAILABLE")
    # All arguments after the batch path are fixed literals or validated identifiers.
    # subprocess quotes paths containing spaces; otherwise cmd needs literal
    # parentheses escaped. No user-supplied quoting or expansion is accepted.
    command_path = resolved if " " in resolved else resolved.replace("(", "^(").replace(")", "^)")
    return [interpreter, "/d", "/v:off", "/c", command_path, *arguments[1:]]


class BoundedAzureCredential:
    """Factory-owned credential; close cancels acquisition and settles owned resources."""

    def __init__(self, config: AzureCredentialConfig) -> None:
        self._config = config
        self._closed = False
        self._active: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._sdk: ManagedIdentityCredential | None = None
        self._transport: _IdentityTransport | None = None
        self._cached: AccessToken | None = None
        self._selection: tuple[str, ...] | None = None

    @property
    def config(self) -> AzureCredentialConfig:
        return self._config

    async def __aenter__(self) -> Self:
        if self._closed:
            raise AzureCredentialError("AUTH_UNAVAILABLE")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await self.close()

    def _managed_selection(self) -> None:
        config = self.config
        values = {name: os.environ.get(name, "") for name in _ENV_SELECTORS}
        permitted = (
            {"IDENTITY_ENDPOINT", "IDENTITY_HEADER"}
            if config.managed_identity_source == "app_service"
            else set()
        )
        if any(value for key, value in values.items() if key not in permitted):
            raise AzureCredentialError("AUTH_UNAVAILABLE")
        if config.managed_identity_source == "app_service" and (
            values["IDENTITY_ENDPOINT"] != config.managed_identity_endpoint
            or not values["IDENTITY_HEADER"]
        ):
            raise AzureCredentialError("AUTH_UNAVAILABLE")
        current = tuple(values.values())
        if self._selection is not None and self._selection != current:
            raise AzureCredentialError("AUTH_UNAVAILABLE")
        self._selection = current

    async def _cli(self) -> AccessToken:
        if not _cli_supported():
            raise AzureCredentialError("AUTH_UNAVAILABLE")
        executable = shutil.which("az.cmd" if sys.platform == "win32" else "az")
        if executable is None:
            raise AzureCredentialError("AUTH_UNAVAILABLE")
        arguments = [
            executable,
            "account",
            "get-access-token",
            "--output",
            "json",
            "--resource",
            self.config.scope.removesuffix("/.default"),
            "--subscription",
            self.config.subscription_id,
        ]
        environment = dict(os.environ, AZURE_CORE_NO_COLOR="true")
        if sys.platform == "win32":
            arguments = _windows_cli_command(arguments)
            environment["COMSPEC"] = arguments[0]
        job = _WindowsJob() if sys.platform == "win32" else None
        creation = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=8192,
                start_new_session=sys.platform != "win32",
                creationflags=4 if sys.platform == "win32" else 0,  # CREATE_SUSPENDED
                cwd=ntpath.dirname(arguments[0]) if sys.platform == "win32" else "/",
                env=environment,
            )
        )
        process: asyncio.subprocess.Process | None = None
        reads: list[asyncio.Task[bytes]] = []
        try:
            process = await asyncio.shield(creation)
            if job is not None:
                job.attach_and_resume(process.pid)
            assert process.stdout is not None and process.stderr is not None
            reads = [
                asyncio.create_task(_read_pipe(process.stdout, self.config.max_response_bytes)),
                asyncio.create_task(_read_pipe(process.stderr, self.config.max_response_bytes)),
            ]
            stdout, _ = await asyncio.gather(*reads)
            if await process.wait() != 0:
                raise AzureCredentialError("AUTH")
            payload = _json(stdout)
            if not (
                isinstance(payload, dict)
                and isinstance(payload.get("accessToken"), str)
                and type(payload.get("expires_on")) is int
            ):
                raise AzureCredentialError("AUTH")
            return AccessToken(payload["accessToken"], payload["expires_on"])
        finally:

            async def cleanup() -> None:
                try:
                    owned = process if process is not None else await creation
                except OSError:
                    if job is not None:
                        job.close()
                    raise
                try:
                    await _terminate_cli(owned, job)
                finally:
                    for read in reads:
                        read.cancel()
                    await asyncio.gather(*reads, return_exceptions=True)
                    # Release paused pipe transports without accumulating output.
                    async with asyncio.timeout(5):
                        for reader in (owned.stdout, owned.stderr):
                            if reader is not None:
                                try:
                                    while await reader.read(8192):
                                        pass
                                except ConnectionResetError:
                                    pass
                        await owned.wait()

            await _settle(cleanup())

    async def get_token(
        self,
        *scopes: str,
        claims: str | None = None,
        tenant_id: str | None = None,
        **kwargs: Any,
    ) -> AccessToken:
        if (
            self._closed
            or scopes != (self.config.scope,)
            or claims is not None
            or tenant_id not in (None, self.config.tenant_id)
            or kwargs
        ):
            raise AzureCredentialError("AUTH_UNAVAILABLE")
        task = asyncio.current_task()
        if task is None:
            raise AzureCredentialError("AUTH_UNAVAILABLE")
        self._active.add(task)
        private = _private_identity.set(True)
        _filter_logs()
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                async with self._lock:
                    if self._closed or self.config.mode == "workload_identity":
                        # SDK assertion file reads are synchronous and unbounded.
                        # No file read, thread pool, token exchange or fallback is allowed.
                        raise AzureCredentialError("AUTH_UNAVAILABLE")
                    if self.config.mode == "managed_identity":
                        self._managed_selection()
                    elif not _cli_supported():
                        raise AzureCredentialError("AUTH_UNAVAILABLE")
                    if self._cached is not None and self._cached.expires_on > time.time() + 300:
                        return self._cached
                    if self.config.mode == "azure_cli":
                        token = await self._cli()
                    else:
                        if self._sdk is None:
                            self._transport = _IdentityTransport(self.config)
                            self._sdk = ManagedIdentityCredential(
                                client_id=self.config.client_id or None,
                                transport=self._transport,
                                retry_total=0,
                                logging_enable=False,
                            )
                            _filter_logs()
                        try:
                            token = await self._sdk.get_token(self.config.scope)
                        except ClientAuthenticationError:
                            # IMDS wraps transport exceptions; retain only our safe
                            # bounded failure code, never its response/error text.
                            if self._transport is not None and self._transport.failure is not None:
                                raise self._transport.failure from None
                            raise
                    validate_token_routing(token, self.config)
                    self._cached = token
                    return token
        except AzureCredentialError:
            raise
        except (httpx.TimeoutException, TimeoutError):
            raise AzureCredentialError("TIMEOUT") from None
        except (httpx.HTTPError, ServiceRequestError, ServiceResponseError):
            raise AzureCredentialError("NETWORK") from None
        except (ClientAuthenticationError, OSError, ValueError):
            raise AzureCredentialError("AUTH") from None
        finally:
            _private_identity.reset(private)
            self._active.discard(task)

    async def close(self) -> None:
        async with self._close_lock:
            self._closed = True
            tasks = list(self._active)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._cached = None
            if self._sdk is not None:
                await self._sdk.close()
                self._sdk = None
            if self._transport is not None:
                await self._transport.close()
                self._transport = None


def create_azure_credential(config: AzureCredentialConfig) -> BoundedAzureCredential:
    """Create one owned credential. The caller must close it; no I/O at construction."""
    return BoundedAzureCredential(config)
