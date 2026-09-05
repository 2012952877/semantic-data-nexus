from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pyarrow as pa
from query_runtime.domain import SourceFragment
from query_runtime.errors import ResolverFailure
from query_runtime.operators import ResourceLimits
from query_runtime.resolver import ExecutionContext
from semantic_data_nexus_databricks.auth import BearerToken, BearerTokenProvider
from semantic_data_nexus_databricks.client import StatementExecutionClient
from semantic_data_nexus_databricks.config import ResolverConfig
from semantic_data_nexus_databricks.models import (
    ParameterType,
    PhysicalSourceFragment,
    StatementParameter,
)
from semantic_data_nexus_databricks.resolver import DatabricksResolver
from semantic_data_nexus_databricks.transport import AsyncHttpTransport

from nexus_plugins.base import GovernedResolver
from nexus_plugins.compiler import compile_read, rows_to_arrow
from nexus_plugins.contracts import Asset, CredentialProvider, CredentialReference, PluginDescriptor


class ReferencedBearerToken(BearerTokenProvider):
    def __init__(self, reference: CredentialReference, credentials: CredentialProvider) -> None:
        self._reference = reference
        self._credentials = credentials

    async def get_token(self) -> BearerToken:
        values = await self._credentials.resolve(self._reference)
        if set(values) != {"token"}:
            raise ResolverFailure("CREDENTIAL_INVALID", "Databricks requires one referenced token")
        return BearerToken(values["token"])


class DatabricksPlugin(GovernedResolver):
    def __init__(
        self,
        assets: Sequence[Asset],
        config: ResolverConfig,
        credential_ref: CredentialReference,
        credentials: CredentialProvider,
        *,
        limits: ResourceLimits | None = None,
        transport: AsyncHttpTransport | None = None,
    ) -> None:
        super().__init__(
            PluginDescriptor(
                version="nexus-plugins/v1",
                id="databricks-sql",
                kind="resolver",
                runtime_versions=("query-runtime/v0", "query-runtime/v1"),
                interchange=("arrow",),
                credential_ref=credential_ref,
            ),
            assets,
            limits,
        )
        bounded_config = replace(
            config,
            row_limit=min(config.row_limit, self.limits.max_rows + 1),
            byte_limit=min(config.byte_limit, self.limits.max_bytes),
            statement_timeout_seconds=min(
                config.statement_timeout_seconds, self.limits.node_timeout_seconds
            ),
        )
        self._client = StatementExecutionClient(
            bounded_config, ReferencedBearerToken(credential_ref, credentials), transport=transport
        )
        self._resolver = DatabricksResolver(self._client)
        self._statements: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _execute(
        self,
        context: ExecutionContext,
        fragment: SourceFragment,
        asset: Asset,
    ) -> pa.Table:
        compiled = compile_read(fragment, asset, "databricks", self.limits)

        async def submitted(statement_id: str) -> None:
            self._statements[context.cancellation_handle] = statement_id

        try:
            result = await self._resolver.resolve(
                PhysicalSourceFragment(
                    source_name=asset.source.object_name,
                    sql=compiled.sql,
                    parameters=tuple(
                        _parameter(f"p{i}", value) for i, value in enumerate(compiled.values)
                    ),
                ),
                on_statement_submitted=submitted,
            )
            if [column.name for column in result.columns] != compiled.schema.names:
                raise ResolverFailure("SOURCE_SCHEMA_MISMATCH", "Databricks schema drift detected")
            for column, expected in zip(result.columns, compiled.schema, strict=True):
                if _arrow_type(column.type_text) != expected.type:
                    raise ResolverFailure(
                        "SOURCE_SCHEMA_MISMATCH", "Databricks type drift detected"
                    )
            return rows_to_arrow(list(result.rows), compiled.schema)
        finally:
            self._statements.pop(context.cancellation_handle, None)

    async def _cancel_remote(self, handle: str) -> None:
        statement_id = self._statements.get(handle)
        if statement_id is not None:
            await self._client.cancel(statement_id)


def _parameter(name: str, value: Any) -> StatementParameter:
    if isinstance(value, bool):
        return StatementParameter(name, ParameterType.BOOLEAN, value)
    if isinstance(value, int):
        return StatementParameter(name, ParameterType.LONG, value)
    if isinstance(value, float):
        return StatementParameter(name, ParameterType.DOUBLE, value)
    if isinstance(value, Decimal):
        scale = max(0, -int(value.as_tuple().exponent))
        return StatementParameter.decimal(name, value, precision=38, scale=scale)
    # Explicit SQL casts preserve the typed date/decimal/timestamp expression.
    return StatementParameter.string(name, value)


def _arrow_type(value: str) -> pa.DataType:
    import re

    simple = {
        "BOOLEAN": pa.bool_(),
        "BYTE": pa.int8(),
        "TINYINT": pa.int8(),
        "SHORT": pa.int16(),
        "SMALLINT": pa.int16(),
        "INT": pa.int32(),
        "INTEGER": pa.int32(),
        "LONG": pa.int64(),
        "BIGINT": pa.int64(),
        "FLOAT": pa.float32(),
        "DOUBLE": pa.float64(),
        "STRING": pa.string(),
        "DATE": pa.date32(),
        "TIMESTAMP": pa.timestamp("us"),
        "TIMESTAMP_NTZ": pa.timestamp("us"),
    }
    normalized = value.upper().replace(" ", "")
    if normalized in simple:
        return simple[normalized]
    decimal = re.fullmatch(r"DECIMAL\((\d+),(\d+)\)", normalized)
    if decimal:
        return pa.decimal128(int(decimal[1]), int(decimal[2]))
    raise ResolverFailure("SOURCE_TYPE_UNSUPPORTED", "Databricks type is not a supported scalar")
