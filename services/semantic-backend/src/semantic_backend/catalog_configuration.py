"""Reviewed server-side catalogs/bindings; never supplied by a query request."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pyarrow as pa
from nexus_plugins.base import GovernedResolver
from nexus_plugins.compiler import validate_fragment_shape
from nexus_plugins.contracts import Asset, PluginDescriptor
from nexus_plugins.runtime import ResolverRegistry
from pydantic import Field
from query_runtime.domain import OperatorKind, ScalarType, SourceFragment
from query_runtime.operators import ResourceLimits
from query_runtime.resolver import ExecutionContext
from semantic_api.catalog_v1.catalog import PublishedCatalogs, pin_for, value_matches
from semantic_api.catalog_v1.models import (
    CatalogDocument,
    CompilerFailure,
    Frozen,
    ResourceVersion,
    Value,
)
from semantic_api.structured_provider import exact_json

from semantic_backend.catalog_compilation import TYPES, CatalogBindings


class CatalogEntry(Frozen):
    document: CatalogDocument
    bindings: CatalogBindings
    rows: dict[str, list[dict[str, Value | None]]]


class CatalogConfiguration(Frozen):
    contract_version: Literal["catalog-server/v1"]
    entries: tuple[CatalogEntry, ...] = Field(min_length=1, max_length=32)


def source_timestamp(value: str, *, naive_utc: bool) -> datetime:
    match = re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}"
        r"(?:[.,](?P<fraction>\d+))?(?:[Zz]|[+-]\d{2}:\d{2})?",
        value,
    )
    if match is None:
        raise CompilerFailure("SOURCE_TYPE_MISMATCH")
    fraction = match.group("fraction") or ""
    if any(digit != "0" for digit in fraction[6:]):
        raise CompilerFailure("SOURCE_TIME_PRECISION")
    try:
        instant = datetime.fromisoformat(value[:-1] + "Z" if value.endswith("z") else value)
    except ValueError:
        raise CompilerFailure("SOURCE_TYPE_MISMATCH") from None
    if instant.tzinfo is None:
        if not naive_utc:
            raise CompilerFailure("SOURCE_TIMEZONE_REQUIRED")
        instant = instant.replace(tzinfo=UTC)
    return instant


class SyntheticCatalogResolver(GovernedResolver):
    def __init__(
        self, assets: tuple[Asset, ...], tables: dict[str, pa.Table], limits: ResourceLimits
    ):
        super().__init__(
            PluginDescriptor(
                version="nexus-plugins/v1",
                id="catalog-synthetic",
                kind="resolver",
                runtime_versions=("query-runtime/v0", "query-runtime/v1"),
                interchange=("arrow",),
            ),
            assets,
            limits,
        )
        self.tables = tables

    async def _execute(
        self, context: ExecutionContext, fragment: SourceFragment, asset: Asset
    ) -> pa.Table:
        validate_fragment_shape(fragment)
        table = self.tables[asset.source.alias]
        for operation in fragment.operations:
            if operation.kind is not OperatorKind.SOURCE:
                table = await self._bounds.execute(
                    operation, (table,), self._active[context.cancellation_handle]
                )
        return table


class CatalogRegistry:
    def __init__(self, configuration: CatalogConfiguration):
        self.catalogs = PublishedCatalogs(tuple(e.document for e in configuration.entries))
        self._entries: dict[ResourceVersion, CatalogEntry] = {}
        self._tables: dict[ResourceVersion, dict[str, pa.Table]] = {}
        for entry in configuration.entries:
            pin = pin_for(entry.document)
            if (
                entry.bindings.catalog != pin
                or entry.bindings.content_sha256 != entry.document.bindings_sha256
            ):
                raise CompilerFailure("BINDING_PIN_MISMATCH")
            if {b.entity_id for b in entry.bindings.entities} != {
                e.id for e in entry.document.entities
            } or len(entry.bindings.entities) != len(entry.document.entities):
                raise CompilerFailure("BINDING_MISSING")
            if set(entry.rows) != {b.source.alias for b in entry.bindings.entities}:
                raise CompilerFailure("SOURCE_SCHEMA_MISMATCH")
            fields = {f.id: f for f in entry.document.fields}
            tables = {}
            for binding in entry.bindings.entities:
                if binding.source.source_type != "synthetic":
                    raise CompilerFailure("CATALOG_RESOLVER_NOT_CONFIGURED")
                if {f.field_id for f in binding.fields} != {
                    f.id for f in fields.values() if f.entity_id == binding.entity_id
                } or len({f.field_id for f in binding.fields}) != len(binding.fields):
                    raise CompilerFailure("BINDING_MISSING")
                names = [f.column_name for f in binding.fields]
                if len({name.casefold() for name in names}) != len(names):
                    raise CompilerFailure("BINDING_AMBIGUOUS")
                rows = entry.rows[binding.source.alias]
                if len(rows) > 100_000 or any(set(row) != set(names) for row in rows):
                    raise CompilerFailure("SOURCE_SCHEMA_MISMATCH")
                arrays = []
                for column in binding.fields:
                    if column.data_type != TYPES[fields[column.field_id].data_type]:
                        raise CompilerFailure("BINDING_TYPE_OR_FIELD")
                    values = [row[column.column_name] for row in rows]
                    arrow_type = {
                        ScalarType.STRING: pa.string(),
                        ScalarType.INTEGER: pa.int64(),
                        ScalarType.FLOAT: pa.float64(),
                        ScalarType.BOOLEAN: pa.bool_(),
                        ScalarType.TIMESTAMP: pa.timestamp("us", tz="UTC"),
                    }.get(column.data_type)
                    if arrow_type is None:
                        raise CompilerFailure("BINDING_TYPE_OR_FIELD")
                    if column.data_type is ScalarType.TIMESTAMP:
                        parsed: list[datetime | None] = []
                        for value in values:
                            if value is None:
                                parsed.append(None)
                                continue
                            if not isinstance(value, str):
                                raise CompilerFailure("SOURCE_TYPE_MISMATCH")
                            instant = source_timestamp(
                                value, naive_utc=column.naive_timestamp_timezone == "UTC"
                            )
                            parsed.append(instant)
                        arrays.append(pa.array(parsed, type=arrow_type, safe=True))
                    else:
                        logical_type = fields[column.field_id].data_type
                        if any(
                            value is not None
                            and (
                                not value_matches(value, logical_type)
                                or (
                                    logical_type == "number"
                                    and isinstance(value, (int, float))
                                    and (
                                        not math.isfinite(value)
                                        or (type(value) is int and int(float(value)) != value)
                                    )
                                )
                            )
                            for value in values
                        ):
                            raise CompilerFailure("SOURCE_TYPE_MISMATCH")
                        arrays.append(pa.array(values, type=arrow_type, safe=True))
                tables[binding.source.alias] = pa.Table.from_arrays(arrays, names=names)
                Asset(binding.source, tables[binding.source.alias].schema)
            self._entries[pin] = entry
            self._tables[pin] = tables

    @classmethod
    def load(cls, path: Path) -> CatalogRegistry:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise CompilerFailure("CATALOG_CONFIGURATION_LIMIT")
        return cls(CatalogConfiguration.model_validate(exact_json(path.read_bytes())))

    def entry(self, pin: ResourceVersion) -> CatalogEntry:
        entry = self._entries.get(pin)
        if entry is None:
            raise CompilerFailure("RESOURCE_NOT_AVAILABLE")
        return entry

    def resolver(
        self, pin: ResourceVersion, entities: set[str], limits: ResourceLimits
    ) -> ResolverRegistry:
        entry = self.entry(pin)
        tables = self._tables[pin]
        assets = tuple(
            Asset(b.source, tables[b.source.alias].schema)
            for b in entry.bindings.entities
            if b.entity_id in entities
        )
        return ResolverRegistry([SyntheticCatalogResolver(assets, tables, limits)])
