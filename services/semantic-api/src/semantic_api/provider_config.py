from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from semantic_api.models import ProviderSelection
from semantic_api.provider import CompilerProvider, ProviderError, StaticFixtureProvider

# Deliberately one production protocol/origin. Custom gateways and Azure variants
# need their own reviewed transport, DNS/egress policy and protocol tests.
OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"


class ProviderConfigError(ValueError):
    def __init__(self) -> None:
        super().__init__("Invalid server-side compiler provider configuration.")


class ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    mode: ProviderSelection = ProviderSelection.STATIC
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

    @model_validator(mode="after")
    def check_endpoint(self) -> ProviderSettings:
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
        from semantic_api.structured_provider import StructuredHTTPProvider

        self.settings = settings
        self.closed = False
        self.real: StructuredHTTPProvider | None = None
        if settings.mode is ProviderSelection.OPENAI_COMPATIBLE:
            credential = env.get(settings.credential_env, "")
            if (
                not credential
                or len(credential) > 4_096
                or any(ord(char) < 33 or ord(char) > 126 for char in credential)
            ):
                raise ProviderConfigError()
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
