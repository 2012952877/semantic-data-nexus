from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import suppress

import pyarrow as pa
from query_runtime.domain import CapabilityCatalog, OperatorKind, OperatorSpec, SourceFragment
from query_runtime.errors import ResolverFailure, ResourceLimitFailure
from query_runtime.operators import DuckDBOperatorExecutor, ResourceLimits
from query_runtime.resolver import ExecutionContext

from nexus_plugins.compiler import PUSHDOWN, validate_fragment_shape
from nexus_plugins.contracts import Asset, PluginDescriptor, require_asset
from nexus_plugins.types import predicate_supported


class GovernedResolver(ABC):
    """Extends the existing ParameterizedSourceAdapter, with bounded active handles."""

    def __init__(
        self,
        descriptor: PluginDescriptor,
        assets: Sequence[Asset],
        limits: ResourceLimits | None = None,
    ) -> None:
        if descriptor.kind != "resolver" or not assets:
            raise ValueError("A resolver requires a descriptor and a nonempty asset inventory")
        self.descriptor = descriptor
        self.assets = {asset.source.object_name: asset for asset in assets}
        if len(self.assets) != len(assets):
            raise ValueError("Duplicate asset IDs")
        self.limits = limits or ResourceLimits()
        self._bounds = DuckDBOperatorExecutor(self.limits)
        self._active: dict[str, asyncio.Event] = {}

    def discover(self) -> tuple[Asset, ...]:
        return tuple(self.assets[key] for key in sorted(self.assets))

    def supports_fragment(self, fragment: SourceFragment) -> bool:
        try:
            validate_fragment_shape(fragment)
        except ResolverFailure as exc:
            if exc.code == "PUSHDOWN_UNSUPPORTED":
                return False
            raise
        return all(
            operation.predicate is None or predicate_supported(operation.predicate.expression)
            for operation in fragment.operations
        )

    async def capabilities(self, source_alias: str) -> CapabilityCatalog:
        assets = [a for a in self.assets.values() if a.source.alias == source_alias]
        if not assets:
            raise ResolverFailure("ASSET_DENIED", "Source alias is not registered")
        return CapabilityCatalog(
            source_alias=source_alias,
            source_type=assets[0].source.source_type,
            operator_kinds=PUSHDOWN,
            max_rows=self.limits.max_rows,
            max_bytes=self.limits.max_bytes,
        )

    async def schema(self, asset_id: str) -> pa.Schema:
        asset = self.assets.get(asset_id)
        if asset is None:
            raise ResolverFailure("ASSET_DENIED", "Asset is not registered")
        table = await self.execute_validated_fragment(
            ExecutionContext("schema", "schema", 1, uuid.uuid4().hex),
            SourceFragment(
                source=asset.source,
                operations=(
                    OperatorSpec(kind=OperatorKind.SOURCE),
                    OperatorSpec(kind=OperatorKind.LIMIT, limit=0),
                ),
            ),
            asyncio.Event(),
        )
        return table.schema

    async def connection_test(self) -> None:
        for asset in self.discover():
            await self.schema(asset.source.object_name)

    async def health(self) -> bool:
        await self.connection_test()
        return True

    async def cancel(self, cancellation_handle: str) -> None:
        event = self._active.get(cancellation_handle)
        if event is None:
            raise ResolverFailure("RESOLVER_EXECUTION_UNKNOWN", "Cancellation handle is not active")
        event.set()

    async def execute_validated_fragment(
        self,
        context: ExecutionContext,
        fragment: SourceFragment,
        cancel_event: asyncio.Event,
    ) -> pa.Table:
        asset = require_asset(fragment.source, self.assets)
        handle = context.cancellation_handle
        if handle in self._active:
            raise ResolverFailure("RESOLVER_EXECUTION_DUPLICATE", "Cancellation handle is in use")
        if cancel_event.is_set():
            raise asyncio.CancelledError
        self._active[handle] = cancel_event
        work = asyncio.create_task(self._execute(context, fragment, asset))
        cancelled = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {work, cancelled},
                timeout=self.limits.node_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise ResourceLimitFailure("SOURCE_TIMEOUT", "Source deadline exceeded")
            if cancel_event.is_set():
                raise asyncio.CancelledError
            table = await work
            self._bounds.enforce_limits(table)
            return table
        finally:
            cancelled.cancel()
            try:
                if not work.done():
                    cancel_event.set()
                    try:
                        await self._cancel_remote(handle)
                    finally:
                        work.cancel()
                        with suppress(asyncio.CancelledError):
                            await work
                elif cancel_event.is_set() and not work.cancelled():
                    # Observe a terminal error if cancellation won the completion race.
                    work.exception()
            finally:
                self._active.pop(handle, None)

    async def _cancel_remote(self, handle: str) -> None:
        """Default: task cancellation interrupts the adapter's local operation."""
        return None

    @abstractmethod
    async def _execute(
        self,
        context: ExecutionContext,
        fragment: SourceFragment,
        asset: Asset,
    ) -> pa.Table: ...
