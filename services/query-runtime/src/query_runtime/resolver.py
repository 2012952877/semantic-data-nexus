"""Resolver protocols and deterministic clean-room fake source."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import pyarrow as pa

from query_runtime.domain import CapabilityCatalog, OperatorKind, SourceFragment
from query_runtime.errors import ResolverFailure
from query_runtime.operators import DuckDBOperatorExecutor


@dataclass(frozen=True)
class ExecutionContext:
    run_id: str
    node_id: str
    attempt: int
    cancellation_handle: str


class SourceResolver(Protocol):
    async def execute(
        self,
        context: ExecutionContext,
        fragment: SourceFragment,
        cancel_event: asyncio.Event,
    ) -> pa.Table: ...

    async def cancel(self, cancellation_handle: str) -> None: ...

    async def health(self) -> bool: ...

    async def capabilities(self, source_alias: str) -> CapabilityCatalog: ...


class ParameterizedSourceAdapter(Protocol):
    """Integration seam for a connector such as the independent Databricks package."""

    async def execute_validated_fragment(
        self,
        context: ExecutionContext,
        fragment: SourceFragment,
        cancel_event: asyncio.Event,
    ) -> pa.Table:
        """Map a typed fragment to a parameterized connector request."""
        ...

    async def cancel(self, cancellation_handle: str) -> None: ...

    async def health(self) -> bool: ...

    async def capabilities(self, source_alias: str) -> CapabilityCatalog: ...


class AdapterResolver:
    def __init__(self, adapter: ParameterizedSourceAdapter) -> None:
        self._adapter = adapter

    async def execute(
        self,
        context: ExecutionContext,
        fragment: SourceFragment,
        cancel_event: asyncio.Event,
    ) -> pa.Table:
        return await self._adapter.execute_validated_fragment(
            context, fragment, cancel_event
        )

    async def cancel(self, cancellation_handle: str) -> None:
        await self._adapter.cancel(cancellation_handle)

    async def health(self) -> bool:
        return await self._adapter.health()

    async def capabilities(self, source_alias: str) -> CapabilityCatalog:
        return await self._adapter.capabilities(source_alias)


class FakeResolver:
    """Deterministic resolver used by offline fixtures and tests."""

    def __init__(
        self,
        *,
        tables: dict[str, pa.Table],
        catalogs: dict[str, CapabilityCatalog],
        delays: dict[str, float] | None = None,
        failures: dict[str, str] | None = None,
    ) -> None:
        self._tables = tables
        self._catalogs = catalogs
        self._delays = delays or {}
        self._failures = failures or {}
        self._executor = DuckDBOperatorExecutor()
        self.active = 0
        self.max_active = 0
        self.executions: dict[str, ExecutionContext] = {}
        self.cancelled: list[str] = []

    async def execute(
        self,
        context: ExecutionContext,
        fragment: SourceFragment,
        cancel_event: asyncio.Event,
    ) -> pa.Table:
        self.executions[context.cancellation_handle] = context
        alias = fragment.source.alias
        if alias not in self._tables:
            raise ResolverFailure(
                "RESOLVER_SOURCE_UNKNOWN", f"Fake source '{alias}' is not configured"
            )
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            delay = self._delays.get(alias, 0)
            if delay:
                try:
                    await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                    raise asyncio.CancelledError
                except TimeoutError:
                    pass
            if alias in self._failures:
                raise ResolverFailure("RESOLVER_SOURCE_FAILED", self._failures[alias])
            table = self._tables[alias]
            for operation in fragment.operations:
                if operation.kind is OperatorKind.SOURCE:
                    continue
                table = await self._executor.execute(operation, (table,), cancel_event)
            return table
        finally:
            self.active -= 1

    async def cancel(self, cancellation_handle: str) -> None:
        if cancellation_handle not in self.executions:
            raise ResolverFailure(
                "RESOLVER_EXECUTION_UNKNOWN",
                "Resolver cancellation handle is not active or known",
            )
        self.cancelled.append(cancellation_handle)

    async def health(self) -> bool:
        return True

    async def capabilities(self, source_alias: str) -> CapabilityCatalog:
        try:
            return self._catalogs[source_alias]
        except KeyError as exc:
            raise ResolverFailure(
                "RESOLVER_CAPABILITY_UNKNOWN",
                f"No capabilities configured for '{source_alias}'",
            ) from exc
