"""Stable runtime errors and diagnostics."""

from __future__ import annotations

from typing import Any


class RuntimeFailure(Exception):
    """Base failure carrying a stable, non-secret diagnostic code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class BindingFailure(RuntimeFailure):
    pass


class PlanFailure(RuntimeFailure):
    pass


class OperatorFailure(RuntimeFailure):
    pass


class ResourceLimitFailure(RuntimeFailure):
    pass


class ResultStoreFailure(RuntimeFailure):
    pass

class ResolverFailure(RuntimeFailure):
    pass
    pass
