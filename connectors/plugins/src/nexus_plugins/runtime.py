"""Registration and dispatch through the existing planner/coordinator, never a second DAG engine."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pyarrow as pa
from query_runtime.coordinator import QueryCoordinator, RunOutcome
from query_runtime.domain import (
    CapabilityCatalog,
    CommittedManifest,
    PhysicalPlan,
    ResultHandle,
    SourceFragment,
)
from query_runtime.errors import PlanFailure, ResolverFailure
from query_runtime.events import EventStore
from query_runtime.operators import DuckDBOperatorExecutor, ResourceLimits
from query_runtime.planner import CapabilityPlanner, ConceptBinder, ValidatedLogicalGraph
from query_runtime.resolver import AdapterResolver, ExecutionContext
from query_runtime.result_store import ResultStore

from nexus_plugins.base import GovernedResolver
from nexus_plugins.compiler import validate_fragment_shape
from nexus_plugins.contracts import Asset, PluginDescriptor, require_asset


class DuckDBComputePlugin(DuckDBOperatorExecutor):
    descriptor = PluginDescriptor(
        version="nexus-plugins/v1",
        id="duckdb",
        kind="compute",
        runtime_versions=("query-runtime/v0", "query-runtime/v1"),
        interchange=("arrow",),
    )


class ResultStorePlugin:
    """A version/limit adapter over ResultStore; it does not own durable job state."""

    def __init__(self, store: ResultStore, limits: ResourceLimits | None = None) -> None:
        self.descriptor = PluginDescriptor(
            version="nexus-plugins/v1",
            id="runtime-results",
            kind="result-store",
            runtime_versions=("query-runtime/v0", "query-runtime/v1"),
            interchange=("arrow", "parquet"),
        )
        self.store = store
        self.limits = limits or ResourceLimits()
        self._bounds = DuckDBOperatorExecutor(self.limits)

    async def commit(
        self,
        run_id: str,
        node_id: str,
        table: pa.Table,
        cancel_event: asyncio.Event | None = None,
    ) -> CommittedManifest:
        self._bounds.enforce_limits(table)
        return await self.store.commit(run_id, node_id, table, cancel_event)

    async def read_page(self, handle: ResultHandle, offset: int, limit: int) -> pa.Table:
        if (
            isinstance(offset, bool)
            or isinstance(limit, bool)
            or offset < 0
            or limit < 0
            or limit > self.limits.max_rows
        ):
            raise ResolverFailure("RESULT_PAGE_INVALID", "Page exceeds the configured bounds")
        table = await self.store.read_page(handle, offset, limit)
        self._bounds.enforce_limits(table)
        return table


class ResolverRegistry:
    def __init__(self, resolvers: Sequence[GovernedResolver]) -> None:
        self._routes: dict[str, GovernedResolver] = {}
        self._assets: dict[str, Asset] = {}
        self._active: dict[str, GovernedResolver] = {}
        for resolver in resolvers:
            for asset in resolver.discover():
                alias = asset.source.alias
                if alias in self._routes:
                    raise ValueError("Source aliases must be unique across registered assets")
                self._routes[alias] = resolver
                self._assets[alias] = asset
        if not self._routes:
            raise ValueError("At least one governed source is required")

    async def plan(self, graph: ValidatedLogicalGraph, binder: ConceptBinder) -> PhysicalPlan:
        catalogs = {alias: await self.capabilities(alias) for alias in self._routes}
        return CapabilityPlanner(
            binder=binder,
            sources={k: a.source for k, a in self._assets.items()},
            capabilities=catalogs,
            fragment_supported=lambda fragment: self._route(
                fragment.source.alias
            ).supports_fragment(fragment),
        ).plan(graph)

    def preflight(self, plan: PhysicalPlan) -> None:
        for node in plan.nodes:
            fragment = node.source_fragment
            if fragment is None:
                continue
            resolver = self._route(fragment.source.alias)
            if plan.version not in resolver.descriptor.runtime_versions:
                raise PlanFailure("PLUGIN_VERSION_MISMATCH", "Resolver rejects the plan version")
            require_asset(fragment.source, resolver.assets)
            validate_fragment_shape(fragment)

    def _route(self, alias: str) -> GovernedResolver:
        resolver = self._routes.get(alias)
        if resolver is None:
            raise ResolverFailure("ASSET_DENIED", "No resolver is registered for this source")
        return resolver

    async def execute(
        self,
        context: ExecutionContext,
        fragment: SourceFragment,
        cancel_event: asyncio.Event,
    ) -> pa.Table:
        resolver = self._route(fragment.source.alias)
        if context.cancellation_handle in self._active:
            raise ResolverFailure("RESOLVER_EXECUTION_DUPLICATE", "Cancellation handle is in use")
        self._active[context.cancellation_handle] = resolver
        try:
            return await AdapterResolver(resolver).execute(context, fragment, cancel_event)
        finally:
            self._active.pop(context.cancellation_handle, None)

    async def cancel(self, cancellation_handle: str) -> None:
        resolver = self._active.get(cancellation_handle)
        if resolver is None:
            raise ResolverFailure("RESOLVER_EXECUTION_UNKNOWN", "Cancellation handle is not active")
        await resolver.cancel(cancellation_handle)

    async def capabilities(self, source_alias: str) -> CapabilityCatalog:
        return await self._route(source_alias).capabilities(source_alias)

    async def health(self) -> bool:
        for resolver in set(self._routes.values()):
            await resolver.connection_test()
        return True


class PluginRuntime:
    def __init__(
        self,
        registry: ResolverRegistry,
        compute: DuckDBComputePlugin,
        results: ResultStorePlugin,
        *,
        event_store: EventStore | None = None,
        max_concurrency: int = 2,
    ) -> None:
        self.registry = registry
        self.compute = compute
        self.results = results
        self.coordinator = QueryCoordinator(
            resolver=registry,
            result_store=results,
            event_store=event_store,
            limits=compute.limits,
            max_concurrency=max_concurrency,
        )
        self.coordinator.executor = compute

    async def run(self, plan: PhysicalPlan, *, run_id: str | None = None) -> RunOutcome:
        for plugin in (self.compute, self.results):
            if plan.version not in plugin.descriptor.runtime_versions:
                raise PlanFailure("PLUGIN_VERSION_MISMATCH", "Plugin rejects the plan version")
        self.registry.preflight(plan)
        return await self.coordinator.run(plan, run_id=run_id)

    async def cancel(self, run_id: str) -> bool:
        return await self.coordinator.cancel(run_id)
