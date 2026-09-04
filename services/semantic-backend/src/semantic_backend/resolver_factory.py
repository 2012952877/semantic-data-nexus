from __future__ import annotations

import os
from collections.abc import Mapping

from query_runtime.resolver import FakeResolver, SourceResolver

from semantic_backend.adapter import CompilerRuntimeAdapter
from semantic_backend.fake_source import load_synthetic_sales


class ResolverConfigurationError(RuntimeError):
    pass


def resolver_from_environment(
    adapter: CompilerRuntimeAdapter,
    environment: Mapping[str, str] | None = None,
) -> SourceResolver:
    values = os.environ if environment is None else environment
    mode = values.get("SEMANTIC_NEXUS_RESOLVER", "fake").strip().lower()
    source = adapter.mapping.source
    if mode == "fake":
        from query_runtime.domain import AggregateFunction, CapabilityCatalog, OperatorKind

        catalog = CapabilityCatalog(
            source_alias=source.alias,
            source_type=source.source_type,
            operator_kinds=frozenset(
                {
                    OperatorKind.SELECT,
                    OperatorKind.FILTER,
                    OperatorKind.AGGREGATE,
                    OperatorKind.SORT,
                }
            ),
            aggregate_functions=frozenset(AggregateFunction),
            max_rows=100_000,
            max_bytes=32 * 1024 * 1024,
        )
        return FakeResolver(
            tables={source.alias: load_synthetic_sales()},
            catalogs={source.alias: catalog},
        )
    if mode != "databricks":
        raise ResolverConfigurationError("SEMANTIC_NEXUS_RESOLVER must be 'fake' or 'databricks'")

    required = (
        "DATABRICKS_WORKSPACE_HOST",
        "DATABRICKS_WAREHOUSE_ID",
        "DATABRICKS_TOKEN",
    )
    missing = [name for name in required if not values.get(name, "").strip()]
    if missing:
        raise ResolverConfigurationError(
            "Databricks mode is missing required configuration: " + ", ".join(missing)
        )
    try:
        from semantic_data_nexus_databricks import (
            BearerToken,
            BearerTokenProvider,
            DatabricksResolver,
            ResolverConfig,
            StatementExecutionClient,
        )

        from semantic_backend.databricks_adapter import (
            DatabricksFragmentTranslator,
            DatabricksSourceAdapter,
        )
    except ImportError as exc:
        raise ResolverConfigurationError(
            "Databricks mode requires the semantic-backend databricks extra"
        ) from exc

    config = ResolverConfig(
        workspace_host=values["DATABRICKS_WORKSPACE_HOST"],
        warehouse_id=values["DATABRICKS_WAREHOUSE_ID"],
        catalog=values.get("DATABRICKS_CATALOG", "synthetic_demo"),
        schema=values.get("DATABRICKS_SCHEMA", "analytics"),
        row_limit=1_001,
        byte_limit=10 * 1024 * 1024,
        statement_timeout_seconds=30,
        cancel_on_timeout=True,
    )

    class ConfiguredTokenProvider(BearerTokenProvider):
        def __init__(self, value: str) -> None:
            self._value = value

        async def get_token(self) -> BearerToken:
            return BearerToken(self._value)

        def __repr__(self) -> str:
            return "ConfiguredTokenProvider(<redacted>)"

    client = StatementExecutionClient(
        config,
        ConfiguredTokenProvider(values["DATABRICKS_TOKEN"]),
    )
    connector = DatabricksResolver(client)
    return DatabricksSourceAdapter(
        connector,
        DatabricksFragmentTranslator(
            catalog=config.catalog,
            schema=config.schema,
            row_limit=1_000,
        ),
        timeout_seconds=config.statement_timeout_seconds + 2,
        close=client.aclose,
        provider_cancel=client.cancel,
    )
