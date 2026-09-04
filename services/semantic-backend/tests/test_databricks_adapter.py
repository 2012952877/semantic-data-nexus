from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pyarrow as pa
import pytest
from query_runtime.domain import (
    AggregateFunction,
    AggregateSpec,
    BoundColumn,
    BoundPredicate,
    BoundSource,
    ExpressionKind,
    OperatorKind,
    OperatorSpec,
    ScalarType,
    SourceFragment,
    TypedExpression,
)
from query_runtime.errors import ResolverFailure
from query_runtime.resolver import ExecutionContext, FakeResolver
from semantic_data_nexus_databricks.models import (
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
    assert translated.sql.endswith("LIMIT 1000")
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


def test_resolver_factory_defaults_fake_and_live_fails_closed(monkeypatch) -> None:
    from semantic_backend.adapter import CompilerRuntimeAdapter

    adapter = CompilerRuntimeAdapter()
    monkeypatch.setenv("SEMANTIC_NEXUS_RESOLVER", "databricks")
    assert isinstance(resolver_from_environment(adapter, {}), FakeResolver)
    with pytest.raises(ResolverConfigurationError, match="missing required"):
        resolver_from_environment(
            adapter,
            {"SEMANTIC_NEXUS_RESOLVER": "databricks"},
        )
