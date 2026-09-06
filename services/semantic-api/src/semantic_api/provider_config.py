from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from semantic_api.models import ProviderSelection
from semantic_api.provider import CompilerProvider, ProviderError, StaticFixtureProvider

# Separate protocols: the OpenAI endpoint is never an arbitrary compatible gateway.
OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"
AZURE_PATH = "/openai/v1/chat/completions"
AZURE_SCOPE = "https://ai.azure.com/.default"
AZURE_MODELS = frozenset(
    {
        "gpt-4o-2024-08-06",
        "gpt-4o-2024-11-20",
        "gpt-4o-mini-2024-07-18",
        "gpt-4.1-2025-04-14",
        "gpt-4.1-mini-2025-04-14",
        "gpt-4.1-nano-2025-04-14",
    }
)
_UUID = r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?$"


class ProviderConfigError(ValueError):
    def __init__(self) -> None:
        super().__init__("Invalid server-side compiler provider configuration.")


class AzureAuth(StrEnum):
    MANAGED_IDENTITY = "managed_identity"
    WORKLOAD_IDENTITY = "workload_identity"
    CLI = "azure_cli"
    KEY = "api_key"


class ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    # Azure is server-only; the legacy public request enum cannot select it.
    mode: ProviderSelection | Literal["azure_openai"] = ProviderSelection.STATIC
    endpoint: str = Field(default=OPENAI_ENDPOINT, repr=False)
    model: str = Field(default="", max_length=100, pattern=r"^[a-zA-Z0-9_.-]*$")
    credential_env: str = Field(default="", pattern=r"^([A-Z][A-Z0-9_]{0,127})?$", repr=False)
    allow_local_mock: bool = False
    timeout_seconds: float = Field(default=5, gt=0, le=120, allow_inf_nan=False)
    max_output_tokens: int = Field(default=2_048, ge=1, le=16_384)
    max_input_tokens: int = Field(default=32_768, ge=1_024, le=131_072)
    max_request_bytes: int = Field(default=64 * 1_024, ge=1_024, le=256 * 1_024)
    max_response_bytes: int = Field(default=128 * 1_024, ge=1_024, le=1_024 * 1_024)
    max_concurrent_calls: int = Field(default=4, ge=1, le=16)
    azure_approved_origin: str = Field(
        default="",
        pattern=r"^(https://[a-z0-9][a-z0-9-]{1,62}[a-z0-9]\.openai\.azure\.com)?$",
        repr=False,
    )
    azure_deployment: str = Field(default="", max_length=100, pattern=r"^[a-zA-Z0-9_.-]*$")
    azure_auth: AzureAuth = AzureAuth.MANAGED_IDENTITY
    azure_tenant_id: str = Field(default="", pattern=_UUID)
    azure_client_id: str = Field(default="", pattern=_UUID)
    azure_subscription_id: str = Field(default="", pattern=_UUID)
    azure_token_file: str = Field(default="", max_length=4096, repr=False)

    @property
    def protocol(self) -> str:
        return "azure-openai-v1-chat-json-schema/1" if self.mode == "azure_openai" else "openai-v1"

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            **self.model_dump(mode="json"),
            "protocol": self.protocol,
            "token_scope": AZURE_SCOPE if self.mode == "azure_openai" else None,
        }

    @model_validator(mode="after")
    def check_endpoint(self) -> ProviderSettings:
        if self.mode == "azure_openai":
            if (
                not self.azure_approved_origin
                or self.endpoint != self.azure_approved_origin + AZURE_PATH
                or self.allow_local_mock
                or not self.azure_deployment
                or self.model not in AZURE_MODELS
                or self.max_input_tokens > 128_000
            ):
                raise ProviderConfigError()
            if self.azure_auth is AzureAuth.KEY:
                if not self.credential_env or any(
                    (
                        self.azure_tenant_id,
                        self.azure_client_id,
                        self.azure_subscription_id,
                        self.azure_token_file,
                    )
                ):
                    raise ProviderConfigError()
            else:
                if self.credential_env or not self.azure_tenant_id:
                    raise ProviderConfigError()
                if self.azure_auth is AzureAuth.CLI:
                    if (
                        not self.azure_subscription_id
                        or self.azure_client_id
                        or self.azure_token_file
                    ):
                        raise ProviderConfigError()
                elif self.azure_subscription_id:
                    raise ProviderConfigError()
                elif self.azure_auth is AzureAuth.WORKLOAD_IDENTITY:
                    if not self.azure_client_id or not self.azure_token_file:
                        raise ProviderConfigError()
                elif self.azure_token_file:
                    raise ProviderConfigError()
            return self
        if (
            any(
                (
                    self.azure_approved_origin,
                    self.azure_deployment,
                    self.azure_tenant_id,
                    self.azure_client_id,
                    self.azure_subscription_id,
                    self.azure_token_file,
                )
            )
            or self.azure_auth is not AzureAuth.MANAGED_IDENTITY
        ):
            raise ProviderConfigError()
        if self.mode is ProviderSelection.STATIC:
            if self.model or self.credential_env or self.endpoint != OPENAI_ENDPOINT:
                raise ProviderConfigError()
            return self
        if not self.model or not self.credential_env:
            raise ProviderConfigError()
        if self.endpoint == OPENAI_ENDPOINT:
            return self
        try:
            url = urlsplit(self.endpoint)
            local = (
                self.allow_local_mock
                and url.scheme == "http"
                and url.hostname == "127.0.0.1"
                and url.port is not None
                and 1 <= url.port <= 65_535
                and url.netloc == f"127.0.0.1:{url.port}"
                and url.path == "/v1/chat/completions"
                and not url.query
                and not url.fragment
            )
        except ValueError:
            raise ProviderConfigError() from None
        if not local:
            raise ProviderConfigError()
        return self

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> ProviderSettings:
        prefix = "SEMANTIC_COMPILER_"
        fields = cls.model_fields
        values: dict[str, str] = {}
        for key, value in env.items():
            if key.startswith(prefix):
                field = key.removeprefix(prefix).lower()
                if field not in fields:
                    raise ProviderConfigError()
                values[field] = value
        try:
            return cls.model_validate(values)
        except ValidationError:
            raise ProviderConfigError() from None


class ProviderRuntime:
    """Server-owned selection and lifecycle; legacy request defaults cannot downgrade real mode."""

    def __init__(self, settings: ProviderSettings, env: Mapping[str, str]) -> None:
        from semantic_api.structured_provider import AzureOpenAIProvider, StructuredHTTPProvider

        self.settings = settings
        self.closed = False
        self.real: StructuredHTTPProvider | AzureOpenAIProvider | None = None
        if settings.mode is not ProviderSelection.STATIC:
            credential = env.get(settings.credential_env, "")
            if settings.mode == "azure_openai":
                self.real = AzureOpenAIProvider(settings, credential)
            else:
                self.real = StructuredHTTPProvider(settings, credential)

    @property
    def ready(self) -> bool:
        return not self.closed

    def select(self, selection: ProviderSelection) -> CompilerProvider:
        if self.closed:
            raise ProviderError("PROVIDER_UNAVAILABLE")
        if self.real is not None:
            return self.real
        if selection is not ProviderSelection.STATIC:
            raise ProviderError("PROVIDER_NOT_CONFIGURED")
        return StaticFixtureProvider()

    async def aclose(self) -> None:
        self.closed = True
        if self.real is not None:
            await self.real.aclose()


def runtime_from_environment(env: Mapping[str, str] | None = None) -> ProviderRuntime:
    environment = os.environ if env is None else env
    return ProviderRuntime(ProviderSettings.from_environment(environment), environment)
