from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import psycopg
import pyarrow as pa
from psycopg import sql
from query_runtime.arrow_memory import retained_table_size
from query_runtime.domain import SourceFragment
from query_runtime.errors import ResolverFailure, ResourceLimitFailure
from query_runtime.operators import ResourceLimits
from query_runtime.resolver import ExecutionContext

from nexus_plugins.base import GovernedResolver
from nexus_plugins.compiler import compile_read, rows_to_arrow
from nexus_plugins.contracts import Asset, CredentialProvider, CredentialReference, PluginDescriptor

# Only builtin scalar OIDs, never domains, user types, or implicitly invoked user functions.
_OIDS = {
    16: pa.bool_(),
    20: pa.int64(),
    21: pa.int16(),
    23: pa.int32(),
    25: pa.string(),
    700: pa.float32(),
    701: pa.float64(),
    1042: pa.string(),
    1043: pa.string(),
    1082: pa.date32(),
    1114: pa.timestamp("us"),
    1184: pa.timestamp("us", tz="UTC"),
}


class PostgreSQLResolver(GovernedResolver):
    def __init__(
        self,
        assets: Sequence[Asset],
        credential_ref: CredentialReference,
        credentials: CredentialProvider,
        *,
        limits: ResourceLimits | None = None,
    ) -> None:
        super().__init__(
            PluginDescriptor(
                version="nexus-plugins/v1",
                id="postgresql",
                kind="resolver",
                runtime_versions=("query-runtime/v0", "query-runtime/v1"),
                interchange=("arrow",),
                credential_ref=credential_ref,
            ),
            assets,
            limits,
        )
        self._reference = credential_ref
        self._credentials = credentials
        self._connections: dict[str, psycopg.AsyncConnection[tuple[Any, ...]]] = {}

    async def _execute(
        self,
        context: ExecutionContext,
        fragment: SourceFragment,
        asset: Asset,
    ) -> pa.Table:
        compiled = compile_read(fragment, asset, "postgres", self.limits)
        settings = dict(await self._credentials.resolve(self._reference))
        if set(settings) - {"host", "port", "dbname", "user", "password", "sslmode", "sslrootcert"}:
            raise ResolverFailure("CREDENTIAL_INVALID", "Unsupported server connection setting")
        if not {"host", "dbname", "user"} <= settings.keys():
            raise ResolverFailure("CREDENTIAL_INVALID", "Incomplete server connection settings")
        authentication = settings.get("password")
        try:
            connection = await psycopg.AsyncConnection.connect(
                host=settings["host"],
                port=settings.get("port"),
                dbname=settings["dbname"],
                user=settings["user"],
                password=authentication,
                sslmode=settings.get("sslmode"),
                sslrootcert=settings.get("sslrootcert"),
                connect_timeout=5,
                options=f"-c statement_timeout={int(self.limits.node_timeout_seconds * 1000)} "
                "-c lock_timeout=2000 -c default_transaction_read_only=on",
            )
            self._connections[context.cancellation_handle] = connection
            async with connection:
                await connection.set_read_only(True)
                await connection.execute("SET LOCAL search_path TO pg_catalog")
                await connection.execute("SET LOCAL timezone TO 'UTC'")
                role = await connection.execute(
                    "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication "
                    "OR rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = current_user"
                )
                if await role.fetchone() != (False,):
                    raise ResolverFailure(
                        "POSTGRES_ROLE_UNSAFE", "Use a non-privileged reader role"
                    )
                relation = ".".join('"' + part.replace('"', '""') + '"' for part in asset.table)
                privileges = await connection.execute(
                    "SELECT pg_catalog.has_table_privilege(current_user, %s, "
                    "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'), "
                    "pg_catalog.has_schema_privilege(current_user, %s, 'CREATE')",
                    (relation, asset.table[0]),
                )
                if await privileges.fetchone() != (False, False):
                    raise ResolverFailure("POSTGRES_ROLE_UNSAFE", "Reader has write/DDL privileges")
                relation_kind = await connection.execute(
                    "SELECT relkind FROM pg_catalog.pg_class WHERE oid = %s::regclass", (relation,)
                )
                if await relation_kind.fetchone() not in {("r",), ("p",), ("m",)}:
                    raise ResolverFailure(
                        "POSTGRES_ASSET_UNSUPPORTED",
                        "Only tables and materialized views are allowed",
                    )
                async with connection.cursor(name="nexus_read") as cursor:
                    await cursor.execute(
                        sql.SQL(compiled.sql),
                        {f"p{i}": value for i, value in enumerate(compiled.values)},
                    )
                    if cursor.description is None:
                        raise ResolverFailure("SOURCE_SCHEMA_MISMATCH", "Source returned no schema")
                    fields = []
                    for column in cursor.description:
                        if column.type_code == 1700:
                            if (
                                column.precision is None
                                or column.scale is None
                                or not 1 <= column.precision <= 38
                                or not 0 <= column.scale <= column.precision
                            ):
                                raise ResolverFailure(
                                    "SOURCE_TYPE_UNSUPPORTED",
                                    "NUMERIC must declare precision/scale",
                                )
                            data_type = pa.decimal128(column.precision, column.scale)
                        else:
                            data_type = _OIDS.get(column.type_code)
                            if data_type is None:
                                raise ResolverFailure(
                                    "SOURCE_TYPE_UNSUPPORTED", "Source type is not a builtin scalar"
                                )
                        fields.append(pa.field(column.name, data_type))
                    actual = pa.schema(fields)
                    if actual.names != compiled.schema.names or any(
                        a.type != b.type for a, b in zip(actual, compiled.schema, strict=True)
                    ):
                        raise ResolverFailure(
                            "SOURCE_SCHEMA_MISMATCH", "Source schema drift detected"
                        )
                    tables: list[pa.Table] = []
                    rows = 0
                    size = 0
                    while batch := await cursor.fetchmany(512):
                        table = rows_to_arrow(batch, compiled.schema)
                        rows += table.num_rows
                        size += retained_table_size(table)
                        if rows > self.limits.max_rows or size > self.limits.max_bytes:
                            raise ResourceLimitFailure(
                                "SOURCE_LIMIT", "PostgreSQL output exceeds budget"
                            )
                        tables.append(table)
                    return (
                        pa.concat_tables(tables)
                        if tables
                        else pa.Table.from_batches([], schema=compiled.schema)
                    )
        except psycopg.errors.QueryCanceled as exc:
            event = self._active.get(context.cancellation_handle)
            if event is not None and event.is_set():
                raise asyncio.CancelledError from exc
            raise ResolverFailure("POSTGRES_EXECUTION_FAILED", "PostgreSQL read cancelled") from exc
        except psycopg.Error as exc:
            raise ResolverFailure("POSTGRES_EXECUTION_FAILED", "PostgreSQL read failed") from exc
        finally:
            self._connections.pop(context.cancellation_handle, None)

    async def _cancel_remote(self, handle: str) -> None:
        connection = self._connections.get(handle)
        if connection is not None and not connection.closed:
            await connection.cancel_safe(timeout=2)
