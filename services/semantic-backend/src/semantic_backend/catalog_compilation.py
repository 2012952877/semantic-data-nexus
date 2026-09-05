"""Opt-in v1 library integration; not a public auth endpoint or an M0 adapter replacement."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC
from typing import Literal

import pyarrow as pa
import pyarrow.compute as pc
from pydantic import BaseModel, ConfigDict
from query_runtime.coordinator import QueryCoordinator, RunOutcome
from query_runtime.domain import (
    AggregateFunction,
    AggregateSpec,
    BoundColumn,
    BoundPredicate,
    BoundSource,
    CapabilityCatalog,
    ExecutionState,
    ExpressionKind,
    JoinKey,
    JoinType,
    NamedExpression,
    OperatorKind,
    OperatorSpec,
    PhysicalPlan,
    ScalarType,
    SortDirection,
    SortSpec,
    SourceFragment,
    TypedExpression,
)
from query_runtime.errors import RuntimeFailure
from query_runtime.operators import DuckDBOperatorExecutor, ResourceLimits
from query_runtime.planner import (
    CapabilityPlanner,
    ExactConceptBinder,
    LogicalNode,
    ValidatedLogicalGraph,
)
from query_runtime.resolver import ExecutionContext, SourceResolver
from query_runtime.result_store import InlineResultStore
from semantic_api.catalog_v1.catalog import fingerprint
from semantic_api.catalog_v1.compiler import CatalogCompiler
from semantic_api.catalog_v1.models import (
    SQGV1,
    Aggregate,
    CatalogCompileRequest,
    Comparison,
    Compilation,
    CompilerContext,
    CompilerFailure,
    Filter,
    Join,
    Limit,
    MemberPredicate,
    Project,
    ResourceVersion,
    Scalar,
    Select,
    Sort,
    TimePredicate,
)
from semantic_api.catalog_v1.trust import TrustedContext
from semantic_api.catalog_v1.validator import validate

from semantic_backend.adapter import AdaptedExecution

TYPES: dict[Scalar, ScalarType] = {
    "string": ScalarType.STRING,
    "integer": ScalarType.INTEGER,
    "number": ScalarType.FLOAT,
    "boolean": ScalarType.BOOLEAN,
    "datetime": ScalarType.TIMESTAMP,
}


class FieldBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    field_id: str
    column_name: str
    data_type: ScalarType
    naive_timestamp_timezone: Literal["UTC"] | None = None


class EntityBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    entity_id: str
    source: BoundSource
    fields: tuple[FieldBinding, ...]


class CatalogBindings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    contract_version: Literal["catalog-bindings/v1"]
    catalog: ResourceVersion
    entities: tuple[EntityBinding, ...]

    @property
    def content_sha256(self) -> str:
        return fingerprint(
            {
                "contract_version": "catalog-binding-content/v1",
                "entities": [
                    {
                        "entity_id": asset.entity_id,
                        "alias": asset.source.alias,
                        "source_type": asset.source.source_type,
                        "object_name": asset.source.object_name,
                        "columns": {f.field_id: f.column_name for f in asset.fields},
                        "utc_naive_fields": [
                            f.field_id for f in asset.fields if f.naive_timestamp_timezone == "UTC"
                        ],
                    }
                    for asset in self.entities
                ],
            }
        )


def _require_binding_pin(graph: SQGV1, context: CompilerContext, bindings: CatalogBindings) -> None:
    if (
        bindings.catalog != graph.catalog
        or bindings.content_sha256 != context.semantic_catalog.bindings_sha256
    ):
        raise CompilerFailure("BINDING_PIN_MISMATCH")


def adapt_catalog(
    graph: SQGV1,
    context: CompilerContext,
    bindings: CatalogBindings,
    source_capabilities: dict[str, CapabilityCatalog],
    *,
    run_id: str,
) -> AdaptedExecution:
    validate(graph, context)
    _require_binding_pin(graph, context, bindings)
    entities = {item.entity_id: item for item in bindings.entities}
    if len(entities) != len(bindings.entities) or len(
        {item.source.alias for item in bindings.entities}
    ) != len(bindings.entities):
        raise CompilerFailure("BINDING_AMBIGUOUS")
    fields = {f.id: f for f in context.semantic_catalog.fields}
    metrics = {m.id: m for m in context.semantic_catalog.metrics}
    relations = {r.id: r for r in context.semantic_catalog.relations}
    windows = {w.id: w for w in context.semantic_catalog.time_windows}
    types = {f.id: TYPES[f.data_type] for f in fields.values()}
    nodes: list[LogicalNode] = []
    bound: dict[str, tuple[BoundColumn, ...]] = {}
    sources: dict[str, BoundSource] = {}
    capabilities: dict[str, CapabilityCatalog] = {}

    def col(name: str) -> TypedExpression:
        return TypedExpression.col(name, types[name])

    def binary(
        kind: ExpressionKind, left: TypedExpression, right: TypedExpression
    ) -> TypedExpression:
        return TypedExpression(kind=kind, data_type=ScalarType.BOOLEAN, args=(left, right))

    for node in graph.nodes:
        op = node.operation
        if isinstance(op, Select):
            asset = entities.get(op.entity_id)
            if asset is None:
                raise CompilerFailure("BINDING_MISSING")
            mapping = {item.field_id: item for item in asset.fields}
            if len(mapping) != len(asset.fields) or len(
                {item.column_name.casefold() for item in asset.fields}
            ) != len(asset.fields):
                raise CompilerFailure("BINDING_AMBIGUOUS")
            advertised = source_capabilities.get(asset.source.alias)
            if (
                advertised is None
                or advertised.source_alias != asset.source.alias
                or advertised.source_type != asset.source.source_type
                or OperatorKind.SELECT not in advertised.operator_kinds
            ):
                raise CompilerFailure("SOURCE_CAPABILITY_MISSING")
            expressions = []
            for concept in op.columns:
                binding = mapping.get(concept)
                if (
                    binding is None
                    or binding.data_type != types[concept]
                    or not binding.column_name
                ):
                    raise CompilerFailure("BINDING_TYPE_OR_FIELD")
                bound[concept] = (
                    BoundColumn(
                        concept=concept,
                        source_alias=asset.source.alias,
                        column_name=binding.column_name,
                        data_type=binding.data_type,
                    ),
                )
                expressions.append(
                    NamedExpression(
                        name=concept,
                        expression=TypedExpression.col(binding.column_name, binding.data_type),
                    )
                )
            sources[asset.source.alias] = asset.source
            # Only SELECT is pushed down here. Every other form is locally typed and
            # bounded until its connector-specific conformance has been reviewed.
            capabilities[asset.source.alias] = CapabilityCatalog(
                source_alias=asset.source.alias,
                source_type=asset.source.source_type,
                operator_kinds=frozenset({OperatorKind.SELECT}),
            )
            nodes.append(
                LogicalNode(
                    id=f"_source_{node.id}",
                    operation=OperatorSpec(kind=OperatorKind.SELECT, columns=op.columns),
                    source_alias=asset.source.alias,
                    concepts=op.columns,
                )
            )
            nodes.append(
                LogicalNode(
                    id=node.id,
                    dependencies=(f"_source_{node.id}",),
                    operation=OperatorSpec(
                        kind=OperatorKind.PROJECT, expressions=tuple(expressions)
                    ),
                )
            )
            continue
        if isinstance(op, Filter):
            predicate = op.predicate
            column = col(predicate.column)
            if isinstance(predicate, Comparison):
                kind = {
                    "eq": ExpressionKind.EQUAL,
                    "ne": ExpressionKind.NOT_EQUAL,
                    "gt": ExpressionKind.GREATER_THAN,
                    "gte": ExpressionKind.GREATER_EQUAL,
                    "lt": ExpressionKind.LESS_THAN,
                    "lte": ExpressionKind.LESS_EQUAL,
                }[predicate.operator]
                expression = binary(
                    kind, column, TypedExpression.literal(predicate.value, column.data_type)
                )
            elif isinstance(predicate, MemberPredicate):
                members = {m.id: m.value for m in fields[predicate.column].members}
                clauses = [
                    binary(
                        ExpressionKind.EQUAL,
                        column,
                        TypedExpression.literal(members[member], column.data_type),
                    )
                    for member in predicate.member_ids
                ]
                expression = clauses[0]
                for clause in clauses[1:]:
                    expression = binary(ExpressionKind.OR, expression, clause)
            elif isinstance(predicate, TimePredicate):
                window = windows[predicate.window_id]
                expression = binary(
                    ExpressionKind.AND,
                    binary(
                        ExpressionKind.GREATER_EQUAL,
                        column,
                        TypedExpression.literal(
                            window.start.astimezone(UTC).replace(tzinfo=None).isoformat(),
                            ScalarType.TIMESTAMP,
                        ),
                    ),
                    binary(
                        ExpressionKind.LESS_THAN,
                        column,
                        TypedExpression.literal(
                            window.end_exclusive.astimezone(UTC).replace(tzinfo=None).isoformat(),
                            ScalarType.TIMESTAMP,
                        ),
                    ),
                )
            operation = OperatorSpec(
                kind=OperatorKind.FILTER, predicate=BoundPredicate(expression=expression)
            )
        elif isinstance(op, Join):
            relation = relations[op.relation_id]
            operation = OperatorSpec(
                kind=OperatorKind.JOIN,
                join_type=JoinType(op.join_type),
                join_keys=(JoinKey(left=relation.from_field, right=relation.to_field),),
            )
        elif isinstance(op, Aggregate):
            aggregates = []
            for measure in op.measures:
                metric = metrics[measure.metric_id]
                aggregates.append(
                    AggregateSpec(
                        name=measure.output,
                        function=AggregateFunction(metric.function),
                        expression=col(metric.field_id),
                    )
                )
                types[measure.output] = (
                    ScalarType.INTEGER
                    if metric.function == "count"
                    else ScalarType.FLOAT
                    if metric.function == "avg"
                    else types[metric.field_id]
                )
            operation = OperatorSpec(
                kind=OperatorKind.AGGREGATE, group_by=op.group_by, aggregates=tuple(aggregates)
            )
        elif isinstance(op, Project):
            operation = OperatorSpec(
                kind=OperatorKind.PROJECT,
                expressions=tuple(
                    NamedExpression(name=p.alias, expression=col(p.source)) for p in op.columns
                ),
            )
            types.update({p.alias: types[p.source] for p in op.columns})
        elif isinstance(op, Sort):
            operation = OperatorSpec(
                kind=OperatorKind.SORT,
                sort=tuple(
                    SortSpec(column=k.column, direction=SortDirection(k.direction)) for k in op.keys
                ),
            )
        elif isinstance(op, Limit):
            operation = OperatorSpec(kind=OperatorKind.LIMIT, limit=op.count)
        else:
            raise CompilerFailure("OPERATOR_NOT_ADAPTED")
        nodes.append(LogicalNode(id=node.id, operation=operation, dependencies=node.dependencies))
    return AdaptedExecution(
        graph=ValidatedLogicalGraph(
            id="catalog-" + run_id, nodes=tuple(nodes), output_node_id=graph.output_node_id
        ),
        binder=ExactConceptBinder(bound),
        sources=sources,
        capabilities=capabilities,
        metadata={
            "compiler_contract": "sqg/v1",
            "runtime_contract": "query-runtime/v0",
            "catalog_sha256": graph.catalog.content_sha256,
            "binding_sha256": bindings.content_sha256,
        },
    )


@dataclass(frozen=True)
class CatalogExecution:
    compilation: Compilation
    table: pa.Table
    metadata: dict[str, str]
    plan: PhysicalPlan
    outcome: RunOutcome


class _CatalogResolver:
    """Validate physical types and normalize instants before v0 local timestamp operations."""

    def __init__(
        self,
        resolver: SourceResolver,
        bindings: CatalogBindings,
        limits: ResourceLimits,
        graph: SQGV1,
        context: CompilerContext,
    ) -> None:
        self.resolver = resolver
        self.assets = {e.source.alias: e for e in bindings.entities}
        self.limits = limits
        self._key_executor = DuckDBOperatorExecutor(limits)
        self._pending_work: set[asyncio.Task[pa.Table]] = set()
        self._closed = False
        relations = {r.id: r for r in context.semantic_catalog.relations}
        fields = {
            f.field_id: (asset.source.alias, f.column_name)
            for asset in bindings.entities
            for f in asset.fields
        }
        self.unique_keys: dict[str, set[str]] = {}
        for node in graph.nodes:
            if isinstance(node.operation, Join):
                relation = relations[node.operation.relation_id]
                keys = [relation.to_field]
                if relation.cardinality == "one_to_one":
                    keys.append(relation.from_field)
                for key in keys:
                    alias, column = fields[key]
                    self.unique_keys.setdefault(alias, set()).add(column)

    async def execute(
        self, context: ExecutionContext, fragment: SourceFragment, cancel_event: asyncio.Event
    ) -> pa.Table:
        if self._closed:
            raise asyncio.CancelledError
        source_work = asyncio.create_task(self.resolver.execute(context, fragment, cancel_event))
        self._pending_work.add(source_work)
        source_work.add_done_callback(self._pending_work.discard)
        table = await source_work
        if table.num_rows > self.limits.max_rows or table.nbytes > self.limits.max_bytes:
            raise RuntimeFailure("SOURCE_RESULT_LIMIT", "Source exceeded the configured budget.")
        mapping = {f.column_name: f for f in self.assets[fragment.source.alias].fields}
        expected = [c.column_name for c in fragment.bound_columns]
        if len(table.column_names) != len(expected) or set(table.column_names) != set(expected):
            raise RuntimeFailure(
                "SOURCE_SCHEMA_MISMATCH", "Source schema differs from its binding."
            )
        for index, name in enumerate(table.column_names):
            spec = mapping[name]
            column = table.column(index)
            kind = column.type
            if spec.data_type is ScalarType.TIMESTAMP:
                if not pa.types.is_timestamp(kind):
                    raise RuntimeFailure(
                        "SOURCE_TYPE_MISMATCH", "Source type differs from its binding."
                    )
                if kind.tz is None and spec.naive_timestamp_timezone != "UTC":
                    raise RuntimeFailure(
                        "SOURCE_TIMEZONE_REQUIRED", "Naive timestamps need a reviewed UTC binding."
                    )
                # Arrow removes the timezone while preserving the UTC instant.
                # This avoids DuckDB/session timezone conversions in its v0 TIMESTAMP type.
                try:
                    normalized = column.cast(pa.timestamp("us"), safe=True)
                except pa.ArrowInvalid:
                    raise RuntimeFailure(
                        "SOURCE_TIME_PRECISION",
                        "Timestamps require lossless microsecond precision.",
                    ) from None
                table = table.set_column(index, name, normalized)
            elif not (
                (spec.data_type is ScalarType.STRING and pa.types.is_string(kind))
                or (spec.data_type is ScalarType.INTEGER and pa.types.is_integer(kind))
                or (spec.data_type is ScalarType.FLOAT and pa.types.is_floating(kind))
                or (spec.data_type is ScalarType.BOOLEAN and pa.types.is_boolean(kind))
            ):
                raise RuntimeFailure(
                    "SOURCE_TYPE_MISMATCH", "Source type differs from its binding."
                )
        for key in sorted(self.unique_keys.get(fragment.source.alias, set())):
            if self._closed:
                raise asyncio.CancelledError
            column = table[key]
            if column.null_count:
                raise RuntimeFailure(
                    "JOIN_CARDINALITY_VIOLATION", "Source violates the governed unique join key."
                )
            if (
                table.num_rows
                and pa.types.is_floating(column.type)
                and not pc.all(pc.is_finite(column)).as_py()
            ):
                raise RuntimeFailure("JOIN_KEY_NONFINITE", "Join keys must be finite.")
            count_name = "_key_count" if key.casefold() != "_key_count" else "_key_count_"
            key_work = asyncio.create_task(
                self._key_executor.execute(
                    OperatorSpec(
                        kind=OperatorKind.AGGREGATE,
                        group_by=(key,),
                        aggregates=(
                            AggregateSpec(name=count_name, function=AggregateFunction.COUNT),
                        ),
                    ),
                    (table.select([key]),),
                    cancel_event,
                )
            )
            self._pending_work.add(key_work)
            key_work.add_done_callback(self._pending_work.discard)
            grouped = await key_work
            if grouped.num_rows != table.num_rows:
                raise RuntimeFailure(
                    "JOIN_CARDINALITY_VIOLATION", "Source violates the governed unique join key."
                )
        return table

    async def aclose(self) -> None:
        self._closed = True
        tasks = [task for task in self._pending_work if not task.done()]
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if any(
            isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
            for result in results
        ):
            raise RuntimeFailure(
                "CATALOG_SOURCE_CLEANUP_FAILED", "Source validation did not clean up safely."
            )

    async def cancel(self, cancellation_handle: str) -> None:
        await self.resolver.cancel(cancellation_handle)

    async def health(self) -> bool:
        return await self.resolver.health()

    async def capabilities(self, source_alias: str) -> CapabilityCatalog:
        return await self.resolver.capabilities(source_alias)


async def execute_catalog(
    request: CatalogCompileRequest,
    compilation: Compilation,
    *,
    compiler: CatalogCompiler,
    context: TrustedContext,
    bindings: CatalogBindings,
    resolver: SourceResolver,
    run_id: str,
    deadline: float,
    limits: ResourceLimits,
) -> CatalogExecution:
    """One encompassing caller deadline includes compile/repair and execution.

    The caller supplies the same absolute deadline used by compile/resume; no new
    timeout is allocated here. Resolver credentials never enter compiler context.
    """
    if compilation.status != "compiled" or compilation.graph is None:
        raise CompilerFailure("COMPILATION_NOT_EXECUTABLE")
    if not math.isfinite(deadline) or deadline <= asyncio.get_running_loop().time():
        raise CompilerFailure("COMPILER_DEADLINE")
    async with asyncio.timeout_at(deadline):
        _, authorized, identity = await compiler._context(request, context, compilation.resolutions)
        validate(compilation.graph, authorized)
        _require_binding_pin(compilation.graph, authorized, bindings)
        selected = {
            node.operation.entity_id
            for node in compilation.graph.nodes
            if isinstance(node.operation, Select)
        }
        capabilities = {
            e.source.alias: await resolver.capabilities(e.source.alias)
            for e in bindings.entities
            if e.entity_id in selected
        }
        adapted = adapt_catalog(
            compilation.graph, authorized, bindings, capabilities, run_id=run_id
        )
        plan = CapabilityPlanner(
            binder=adapted.binder, sources=adapted.sources, capabilities=adapted.capabilities
        ).plan(adapted.graph)
        store = InlineResultStore()
        source_resolver = _CatalogResolver(
            resolver, bindings, limits, compilation.graph, authorized
        )
        coordinator = QueryCoordinator(
            resolver=source_resolver,
            result_store=store,
            limits=limits,
        )
        try:
            outcome = await coordinator.run(plan, run_id=run_id)
        finally:
            await source_resolver.aclose()
        if outcome.summary.state is not ExecutionState.SUCCEEDED or outcome.manifest is None:
            raise CompilerFailure(outcome.summary.diagnostic_code or "RUNTIME_FAILED")
        await compiler._unchanged(request, context, identity, compilation.resolutions)
        table = await store.read_page(outcome.manifest.result, 0, limits.max_rows)
        if table.column_names != [column.name for column in compilation.graph.result_schema]:
            raise CompilerFailure("RUNTIME_RESULT_SCHEMA")
        for index, column in enumerate(compilation.graph.result_schema):
            kind = table.column(index).type
            if not (
                (column.data_type == "string" and pa.types.is_string(kind))
                or (column.data_type == "boolean" and pa.types.is_boolean(kind))
                or (column.data_type == "datetime" and pa.types.is_timestamp(kind))
                or (
                    column.data_type == "number"
                    and (
                        pa.types.is_floating(kind)
                        or pa.types.is_integer(kind)
                        or pa.types.is_decimal(kind)
                    )
                )
                or (
                    column.data_type == "integer"
                    and (
                        pa.types.is_integer(kind) or (pa.types.is_decimal(kind) and kind.scale == 0)
                    )
                )
            ):
                raise CompilerFailure("RUNTIME_RESULT_TYPE")
        return CatalogExecution(
            compilation=compilation,
            table=table,
            metadata={
                **adapted.metadata,
                "run_id": run_id,
                "result_id": outcome.manifest.result.result_id,
            },
            plan=plan,
            outcome=outcome,
        )
