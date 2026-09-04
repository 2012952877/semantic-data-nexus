from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass

from .exceptions import AuthenticationError


@dataclass(frozen=True, repr=False)
class BearerToken:
    _value: str

    def __post_init__(self) -> None:
        if not self._value or self._value.isspace():
            raise AuthenticationError("Bearer token is empty")
        if "\r" in self._value or "\n" in self._value:
            raise AuthenticationError("Bearer token contains an invalid line break")

    def authorization_header(self) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {self._value}"}

    def __repr__(self) -> str:
        return "BearerToken(<redacted>)"


class BearerTokenProvider(ABC):
    """Boundary for PAT, OAuth, workload identity, or managed identity token sources."""

    @abstractmethod
    async def get_token(self) -> BearerToken:
        """Return a current in-memory token without persisting it."""


@dataclass(frozen=True)
class EnvironmentPatProvider(BearerTokenProvider):
    variable_name: str = "DATABRICKS_TOKEN"

    async def get_token(self) -> BearerToken:
        value = os.environ.get(self.variable_name)
        if value is None:
            raise AuthenticationError(
                f"Required PAT environment variable {self.variable_name!r} is not set"
            )
        return BearerToken(value)
