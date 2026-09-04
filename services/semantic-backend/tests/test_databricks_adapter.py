from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

import pyarrow as pa
import pytest
from conftest import request_for, wait_for_terminal
from query_runtime.domain import (
    AggregateFunction,
    AggregateSpec,
    BoundColumn,
    BoundPredicate,
    BoundSource,
    ExpressionKind,
    NamedExpression,
    OperatorKind,
    OperatorSpec,
    ScalarType,
    SourceFragment,
    TypedExpression,
)
from query_runtime.errors import ResolverFailure
from query_runtime.resolver import ExecutionContext, FakeResolver
from semantic_data_nexus_databricks import (
    DatabricksResolverError,
    StatementCanceledError,
)
from semantic_data_nexus_databricks.models import (
    DecimalType,
    LineageMetadata,
    ResultColumn,
    TabularResult,
)
from semantic_data_nexus_databricks.resolver import validate_fragment

from semantic_backend.databricks_adapter import (
    DatabricksFragmentTranslator,
    DatabricksSourceAdapter,
)
from semantic_backend.resolver_factory import (
    ResolverConfigurationError,
    resolver_from_environment,
)
from semantic_backend.service import OrchestrationService


def validated_fragment() -> SourceFragment:
    period = TypedExpression.col("period_quarter", ScalarType.TIMESTAMP)
    predicate = BoundPredicate(
        expression=TypedExpression(
            kind=ExpressionKind.AND,
            data_type=ScalarType.BOOLEAN,
            args=(
                TypedExpression(
                    kind=ExpressionKind.GREATER_EQUAL,
                    data_type=ScalarType.BOOLEAN,
                    args=(
                        period,
                        TypedExpression.literal(
                            "2024-01-01T00:00:00Z",
                            ScalarType.TIMESTAMP,
                        ),
                    ),
                ),
                TypedExpression(
                    kind=ExpressionKind.LESS_THAN,
                    data_type=ScalarType.BOOLEAN,
                    args=(
                        period,
                        TypedExpression.literal(
                            "2024-04-01T00:00:00Z",
                            ScalarType.TIMESTAMP,
                        ),
                    ),
                ),
            ),
        )
    )
    return SourceFragment(
        source=BoundSource(
            alias="synthetic_sales",
            source_type="azure_databricks",
            object_name="commerce_sales_record",
        ),
        operations=(
            OperatorSpec(
                kind=OperatorKind.SELECT,
                columns=("region", "period_quarter", "profit"),
            ),
            OperatorSpec(kind=OperatorKind.FILTER, predicate=predicate),
            OperatorSpec(
                kind=OperatorKind.AGGREGATE,
                group_by=("region", "period_quarter"),
                aggregates=(
                    AggregateSpec(
                        name="profit",
                        function=AggregateFunction.SUM,
                        expression=TypedExpression.col("profit", ScalarType.FLOAT),
                    ),
                ),
            ),
        ),
        bound_columns=(
            BoundColumn(
                concept="commerce.sales_record.region",
                source_alias="synthetic_sales",
                column_name="region",
                data_type=ScalarType.STRING,
            ),
        ),
    )


def test_translator_generates_only_parameterized_read_only_sql() -> None:
    translated = DatabricksFragmentTranslator(
        catalog="synthetic_demo",
        schema="analytics",
    ).translate(validated_fragment())
    assert translated.source_name == "synthetic_sales"
    assert translated.sql.startswith("SELECT * FROM (SELECT")
    assert "`synthetic_demo`.`analytics`.`commerce_sales_record`" in translated.sql
    assert ":p0" in translated.sql and ":p1" in translated.sql
    assert [parameter.value for parameter in translated.parameters] == [
        "2024-01-01T00:00:00Z",
        "2024-04-01T00:00:00Z",
    ]
    assert "DELETE" not in translated.sql
    assert translated.sql.endswith("LIMIT 1001")
    validate_fragment(translated)


def test_translator_rejects_unreviewed_identifier_and_local_operator() -> None:
    translator = DatabricksFragmentTranslator(catalog="synthetic_demo", schema="analytics")
    unsafe = validated_fragment().model_copy(
        update={
            "source": BoundSource(
                alias="synthetic_sales",
                source_type="azure_databricks",
                object_name="unsafe;drop",
            )
        }
    )
    with pytest.raises(ResolverFailure, match="identifier"):
        translator.translate(unsafe)
    local = validated_fragment().model_copy(
        update={
            "operations": (
                OperatorSpec(
                    kind=OperatorKind.PIVOT,
                    pivot_column="period",
                    pivot_value="profit",
                    pivot_values=("current",),
                ),
            )
        }
    )
    with pytest.raises(ResolverFailure, match="cannot cross"):
        translator.translate(local)


@dataclass
class StubConnector:
    fragment: object | None = None

    async def resolve(self, fragment):
        self.fragment = fragment
        return TabularResult(
            columns=(
                ResultColumn(name="region", position=0, type_name="STRING", type_text="STRING"),
                ResultColumn(name="profit", position=1, type_name="DOUBLE", type_text="DOUBLE"),
            ),
            rows=(("北辰区", "2334.0"),),
            lineage=LineageMetadata(
                resolver="stub",
                source_name="synthetic_sales",
                statement_id="statement-synthetic",
                physical_fragment_sha256="0" * 64,
            ),
            elapsed_ms=1,
        )


@dataclass
class ServiceConnector:
    async def resolve(self, fragment):
        return TabularResult(
            columns=(
                ResultColumn(name="region", position=0, type_name="STRING", type_text="STRING"),
                ResultColumn(
                    name="period_quarter",
                    position=1,
                    type_name="TIMESTAMP",
                    type_text="TIMESTAMP",
                ),
                ResultColumn(name="profit", position=2, type_name="DOUBLE", type_text="DOUBLE"),
            ),
            rows=(("北辰区", "2024-01-01T00:00:00Z", "2334.0"),),
            lineage=LineageMetadata(
                resolver="azure_databricks_statement_execution",
                source_name="synthetic_sales",
                statement_id="statement-synthetic",
                physical_fragment_sha256="1" * 64,
            ),
            elapsed_ms=1,
        )


async def test_live_adapter_converts_typed_result_to_arrow() -> None:
    connector = StubConnector()
    adapter = DatabricksSourceAdapter(
        connector,
        DatabricksFragmentTranslator(catalog="synthetic_demo", schema="analytics"),
    )
    table = await adapter.execute(
        ExecutionContext(
            run_id="run_00000000000000000000000000000091",
            node_id="source",
            attempt=1,
            cancellation_handle="opaque-handle",
        ),
        validated_fragment(),
        asyncio.Event(),
    )
    assert isinstance(table, pa.Table)
    assert table.to_pylist() == [{"region": "北辰区", "profit": 2334.0}]


async def test_live_adapter_fails_on_sentinel_row_instead_of_silent_truncation() -> None:
    result = await StubConnector().resolve(None)

    @dataclass
    class SentinelConnector:
        async def resolve(self, fragment):
            return TabularResult(
                columns=result.columns,
                rows=result.rows * 1_001,
                lineage=result.lineage,
                elapsed_ms=result.elapsed_ms,
            )

    adapter = DatabricksSourceAdapter(
        SentinelConnector(),
        DatabricksFragmentTranslator(catalog="synthetic_demo", schema="analytics"),
    )
    with pytest.raises(ResolverFailure, match="complete-result row boundary"):
        await adapter.execute(
            ExecutionContext(
                run_id="run_00000000000000000000000000000099",
                node_id="source",
                attempt=1,
                cancellation_handle="opaque-sentinel",
            ),
            validated_fragment(),
            asyncio.Event(),
        )


async def test_live_service_preserves_connector_provenance_and_lineage() -> None:
    closed = asyncio.Event()

    async def close() -> None:
        closed.set()

    resolver = DatabricksSourceAdapter(
        ServiceConnector(),
        DatabricksFragmentTranslator(catalog="synthetic_demo", schema="analytics"),
        close=close,
    )
    service = OrchestrationService(resolver=resolver)
    request = request_for("run_00000000000000000000000000000094")
    try:
        await service.start(request)
        terminal = await wait_for_terminal(service, request.run_id)
        assert terminal.state.value == "Succeeded"
        detail = await service.get_detail(request.run_id)
        source = next(node for node in detail.lineage.nodes if node.kind.value == "source")
        assert source.source_type == "azure_databricks_statement_execution"
        assert source.operation == "azure_databricks_statement_execution"
        artifact = await service.get_integrated_artifact(request.run_id)
        assert artifact.connector_provenance == (
            {
                "run_id": request.run_id,
                "node_id": "physical-sort_profit",
                "resolver": "azure_databricks_statement_execution",
                "source_name": "synthetic_sales",
                "statement_id": "statement-synthetic",
                "physical_fragment_sha256": "1" * 64,
            },
        )
    finally:
        await service.shutdown()
    assert closed.is_set()


def test_decimal_parameter_and_result_preserve_precision_and_scale() -> None:
    predicate = BoundPredicate(
        expression=TypedExpression(
            kind=ExpressionKind.GREATER_EQUAL,
            data_type=ScalarType.BOOLEAN,
            args=(
                TypedExpression.col("amount", ScalarType.DECIMAL),
                TypedExpression.literal("123.4500000000", ScalarType.DECIMAL),
            ),
        )
    )
    fragment = validated_fragment().model_copy(
        update={"operations": (OperatorSpec(kind=OperatorKind.FILTER, predicate=predicate),)}
    )
    translated = DatabricksFragmentTranslator(
        catalog="synthetic_demo",
        schema="analytics",
    ).translate(fragment)
    parameter = translated.parameters[0]
    assert parameter.type == DecimalType(precision=13, scale=10)
    assert parameter.value == Decimal("123.4500000000")

    high_scale = fragment.model_copy(
        update={
            "operations": (
                OperatorSpec(
                    kind=OperatorKind.FILTER,
                    predicate=BoundPredicate(
                        expression=TypedExpression(
                            kind=ExpressionKind.GREATER_EQUAL,
                            data_type=ScalarType.BOOLEAN,
                            args=(
                                TypedExpression.col("amount", ScalarType.DECIMAL),
                                TypedExpression.literal(
                                    "0.1234567890123456789012345678",
                                    ScalarType.DECIMAL,
                                ),
                            ),
                        )
                    ),
                ),
            )
        }
    )
    high_scale_parameter = (
        DatabricksFragmentTranslator(
            catalog="synthetic_demo",
            schema="analytics",
        )
        .translate(high_scale)
        .parameters[0]
    )
    assert high_scale_parameter.type == DecimalType(precision=28, scale=28)
    assert high_scale_parameter.value == Decimal("0.1234567890123456789012345678")

    result = TabularResult(
        columns=(
            ResultColumn(
                name="amount",
                position=0,
                type_name="DECIMAL",
                type_text="DECIMAL(10,2)",
            ),
        ),
        rows=(("1234.50",),),
        lineage=LineageMetadata(
            resolver="stub",
            source_name="synthetic_sales",
            statement_id="statement-synthetic",
            physical_fragment_sha256="0" * 64,
        ),
        elapsed_ms=1,
    )
    table = DatabricksSourceAdapter._to_arrow(result)
    assert table.schema.field("amount").type == pa.decimal128(10, 2)
    assert table.to_pylist() == [{"amount": Decimal("1234.50")}]


def test_boolean_renderer_preserves_mixed_and_or_grouping() -> None:
    def boolean_column(name: str) -> TypedExpression:
        return TypedExpression.col(name, ScalarType.BOOLEAN)

    expression = TypedExpression(
        kind=ExpressionKind.AND,
        data_type=ScalarType.BOOLEAN,
        args=(
            TypedExpression(
                kind=ExpressionKind.OR,
                data_type=ScalarType.BOOLEAN,
                args=(boolean_column("a"), boolean_column("b")),
            ),
            TypedExpression(
                kind=ExpressionKind.OR,
                data_type=ScalarType.BOOLEAN,
                args=(boolean_column("c"), boolean_column("d")),
            ),
        ),
    )
    fragment = validated_fragment().model_copy(
        update={
            "operations": (
                OperatorSpec(
                    kind=OperatorKind.FILTER,
                    predicate=BoundPredicate(expression=expression),
                ),
            )
        }
    )
    translated = DatabricksFragmentTranslator(
        catalog="synthetic_demo",
        schema="analytics",
    ).translate(fragment)
    assert "(`a` OR `b`) AND CASE WHEN `c` OR `d`" in translated.sql
    validate_fragment(translated)


def test_boolean_renderer_preserves_three_valued_null_semantics() -> None:
    import duckdb

    translator = DatabricksFragmentTranslator(
        catalog="synthetic_demo",
        schema="analytics",
    )
    nested = TypedExpression(
        kind=ExpressionKind.NOT,
        data_type=ScalarType.BOOLEAN,
        args=(
            TypedExpression(
                kind=ExpressionKind.AND,
                data_type=ScalarType.BOOLEAN,
                args=(
                    TypedExpression.col("a", ScalarType.BOOLEAN),
                    TypedExpression(
                        kind=ExpressionKind.OR,
                        data_type=ScalarType.BOOLEAN,
                        args=(
                            TypedExpression.col("b", ScalarType.BOOLEAN),
                            TypedExpression.col("c", ScalarType.BOOLEAN),
                        ),
                    ),
                ),
            ),
        ),
    )
    is_null = TypedExpression(
        kind=ExpressionKind.IS_NULL,
        data_type=ScalarType.BOOLEAN,
        args=(
            TypedExpression(
                kind=ExpressionKind.OR,
                data_type=ScalarType.BOOLEAN,
                args=(
                    TypedExpression.col("c", ScalarType.BOOLEAN),
                    TypedExpression.col("b", ScalarType.BOOLEAN),
                ),
            ),
        ),
    )
    fragment = validated_fragment().model_copy(
        update={
            "operations": (
                OperatorSpec(
                    kind=OperatorKind.SELECT,
                    expressions=(
                        NamedExpression(name="nested", expression=nested),
                        NamedExpression(name="is_null", expression=is_null),
                    ),
                ),
            )
        }
    )
    validate_fragment(translator.translate(fragment))
    connection = duckdb.connect()
    try:
        nested_sql = translator._expression(nested).replace("`", '"')
        is_null_sql = translator._expression(is_null).replace("`", '"')
        row = connection.execute(
            "SELECT "
            f"{nested_sql}, "
            f"{is_null_sql} "
            "FROM (SELECT TRUE AS a, NULL::BOOLEAN AS b, FALSE AS c)"
        ).fetchone()
    finally:
        connection.close()
    assert row == (None, True)


def test_arithmetic_renderer_preserves_nested_rhs_grouping() -> None:
    expression = TypedExpression(
        kind=ExpressionKind.ADD,
        data_type=ScalarType.FLOAT,
        args=(
            TypedExpression.col("a", ScalarType.FLOAT),
            TypedExpression(
                kind=ExpressionKind.SUBTRACT,
                data_type=ScalarType.FLOAT,
                args=(
                    TypedExpression.col("b", ScalarType.FLOAT),
                    TypedExpression.col("c", ScalarType.FLOAT),
                ),
            ),
        ),
    )
    fragment = validated_fragment().model_copy(
        update={
            "operations": (
                OperatorSpec(
                    kind=OperatorKind.SELECT,
                    expressions=(NamedExpression(name="value", expression=expression),),
                ),
            )
        }
    )
    translated = DatabricksFragmentTranslator(
        catalog="synthetic_demo",
        schema="analytics",
    ).translate(fragment)
    assert "`a` + (`b` - `c`)" in translated.sql
    validate_fragment(translated)


@dataclass
class BlockingConnector:
    started: asyncio.Event
    release: asyncio.Event
    cleaned: asyncio.Event

    async def resolve(self, fragment):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cleaned.set()
            raise
        return await StubConnector().resolve(fragment)


@dataclass
class AcknowledgedCancelConnector:
    started: asyncio.Event
    release: asyncio.Event

    async def resolve(self, fragment):
        self.started.set()
        await self.release.wait()
        raise StatementCanceledError("statement-synthetic")


@dataclass
class CancelRaceFailureConnector:
    adapter_task: asyncio.Task | None = None

    async def resolve(self, fragment):
        assert self.adapter_task is not None
        asyncio.get_running_loop().call_soon(self.adapter_task.cancel)
        raise DatabricksResolverError("synthetic provider failure")


async def test_completed_provider_failure_is_not_discarded_by_task_cancel() -> None:
    connector = CancelRaceFailureConnector()
    adapter = DatabricksSourceAdapter(
        connector,
        DatabricksFragmentTranslator(catalog="synthetic_demo", schema="analytics"),
        timeout_seconds=1,
    )
    context = ExecutionContext(
        run_id="run_00000000000000000000000000000098",
        node_id="source",
        attempt=1,
        cancellation_handle="opaque-failure-race",
    )
    task = asyncio.create_task(adapter.execute(context, validated_fragment(), asyncio.Event()))
    connector.adapter_task = task
    with pytest.raises(ResolverFailure, match="could not confirm a clean cancellation"):
        await task
    with pytest.raises(ResolverFailure, match="could not confirm a clean cancellation"):
        await adapter.wait_for_run_cleanup(context.run_id)
    await adapter.aclose()


async def test_completed_provider_cancel_is_acknowledged() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    adapter = DatabricksSourceAdapter(
        AcknowledgedCancelConnector(started, release),
        DatabricksFragmentTranslator(catalog="synthetic_demo", schema="analytics"),
        timeout_seconds=1,
    )
    context = ExecutionContext(
        run_id="run_00000000000000000000000000000097",
        node_id="source",
        attempt=1,
        cancellation_handle="opaque-acknowledged",
    )
    task = asyncio.create_task(adapter.execute(context, validated_fragment(), asyncio.Event()))
    await started.wait()
    await adapter.cancel(context.cancellation_handle)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await adapter.wait_for_run_cleanup(context.run_id)
    await adapter.aclose()


async def test_cancellation_waits_for_connector_cleanup_and_close() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()
    closed = asyncio.Event()

    async def close() -> None:
        closed.set()

    adapter = DatabricksSourceAdapter(
        BlockingConnector(started, release, cleaned),
        DatabricksFragmentTranslator(catalog="synthetic_demo", schema="analytics"),
        timeout_seconds=1,
        close=close,
    )
    context = ExecutionContext(
        run_id="run_00000000000000000000000000000092",
        node_id="source",
        attempt=1,
        cancellation_handle="opaque-cancel",
    )
    task = asyncio.create_task(adapter.execute(context, validated_fragment(), asyncio.Event()))
    await started.wait()
    await adapter.cancel(context.cancellation_handle)
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.provenance(context.run_id)[0].statement_id == "statement-synthetic"
    await adapter.aclose()
    assert closed.is_set()


async def test_unconfirmed_cancellation_cleans_local_connector_task() -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()
    adapter = DatabricksSourceAdapter(
        BlockingConnector(started, asyncio.Event(), cleaned),
        DatabricksFragmentTranslator(catalog="synthetic_demo", schema="analytics"),
        timeout_seconds=0.01,
    )
    context = ExecutionContext(
        run_id="run_00000000000000000000000000000093",
        node_id="source",
        attempt=1,
        cancellation_handle="opaque-timeout",
    )
    task = asyncio.create_task(adapter.execute(context, validated_fragment(), asyncio.Event()))
    await started.wait()
    await adapter.cancel(context.cancellation_handle)
    with pytest.raises(ResolverFailure, match="did not finish cancellation cleanup"):
        await task
    assert cleaned.is_set()
    await adapter.aclose()


async def test_service_does_not_report_unconfirmed_provider_cancel_as_cancelled() -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()
    resolver = DatabricksSourceAdapter(
        BlockingConnector(started, asyncio.Event(), cleaned),
        DatabricksFragmentTranslator(catalog="synthetic_demo", schema="analytics"),
        timeout_seconds=0.01,
    )
    service = OrchestrationService(resolver=resolver)
    request = request_for("run_00000000000000000000000000000095")
    try:
        await service.start(request)
        await started.wait()
        status = await service.cancel(request.run_id)
        assert status.state.value == "Failed"
        assert status.diagnostics[0].code == "DATABRICKS_CANCELLATION_UNCONFIRMED"
        assert cleaned.is_set()
    finally:
        await service.shutdown()


async def test_service_timeout_waits_for_resolver_cleanup(monkeypatch) -> None:
    import semantic_backend.service as service_module

    monkeypatch.setattr(service_module, "_RUN_TIMEOUT_SECONDS", 0.1)
    started = asyncio.Event()
    cleaned = asyncio.Event()
    resolver = DatabricksSourceAdapter(
        BlockingConnector(started, asyncio.Event(), cleaned),
        DatabricksFragmentTranslator(catalog="synthetic_demo", schema="analytics"),
        timeout_seconds=0.2,
    )
    service = OrchestrationService(resolver=resolver)
    request = request_for("run_00000000000000000000000000000100")
    try:
        await service.start(request)
        await asyncio.wait_for(started.wait(), timeout=2)
        status = await wait_for_terminal(service, request.run_id)
        assert status.state.value == "Failed"
        assert cleaned.is_set()
        assert resolver._active_contexts == {}
    finally:
        await service.shutdown()


async def test_resolver_factory_binds_token_to_explicit_mapping(monkeypatch) -> None:
    from semantic_backend.adapter import CompilerRuntimeAdapter

    adapter = CompilerRuntimeAdapter()
    monkeypatch.setenv("SEMANTIC_NEXUS_RESOLVER", "databricks")
    assert isinstance(resolver_from_environment(adapter, {}), FakeResolver)
    with pytest.raises(ResolverConfigurationError, match="missing required"):
        resolver_from_environment(
            adapter,
            {"SEMANTIC_NEXUS_RESOLVER": "databricks"},
        )
    resolver = resolver_from_environment(
        adapter,
        {
            "SEMANTIC_NEXUS_RESOLVER": "databricks",
            "DATABRICKS_WORKSPACE_HOST": "https://synthetic.invalid",
            "DATABRICKS_WAREHOUSE_ID": "synthetic-warehouse",
            "DATABRICKS_TOKEN": "mapped-synthetic-token",
        },
    )
    provider = resolver._resolver._client._auth
    token = await provider.get_token()
    assert token._value == "mapped-synthetic-token"
    assert "mapped-synthetic-token" not in repr(provider)
    await resolver.aclose()
