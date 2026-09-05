from __future__ import annotations

import asyncio
import os
import stat
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import pyarrow as pa
import pyarrow.csv as csv
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from query_runtime.arrow_memory import retained_table_size
from query_runtime.domain import CapabilityCatalog, ExpressionKind, OperatorKind, SourceFragment
from query_runtime.errors import ResolverFailure, ResourceLimitFailure
from query_runtime.operators import ResourceLimits
from query_runtime.resolver import ExecutionContext

from nexus_plugins.base import GovernedResolver
from nexus_plugins.compiler import arrow_value, validate_fragment_shape
from nexus_plugins.contracts import Asset, PluginDescriptor
from nexus_plugins.types import validate_predicate


@dataclass(frozen=True)
class FileAsset:
    asset: Asset
    relative_path: str


@contextmanager
def open_governed(root: Path, relative: str, max_file_bytes: int) -> Iterator[BinaryIO]:
    path = Path(relative)
    if (
        path.is_absolute()
        or path.root
        or path.drive
        or ".." in path.parts
        or ":" in relative
        or "\\" in relative
        or path.suffix not in {".csv", ".parquet"}
    ):
        raise ResolverFailure(
            "FILE_PATH_DENIED", "Only relative CSV/Parquet asset paths are allowed"
        )
    target = root / path
    for candidate in (target, *target.parents):
        status = candidate.lstat()
        if stat.S_ISLNK(status.st_mode) or getattr(status, "st_file_attributes", 0) & 1024:
            raise ResolverFailure("FILE_PATH_DENIED", "Symlinks and reparse points are forbidden")
        if candidate == root:
            break
    expected = target.stat()
    if not stat.S_ISREG(expected.st_mode) or expected.st_size > max_file_bytes:
        raise ResourceLimitFailure("FILE_LIMIT", "Asset must be a bounded regular file")
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        actual = os.fstat(stream.fileno())
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ResolverFailure("FILE_PATH_DENIED", "Asset changed while opening")
        yield stream
        final = os.fstat(stream.fileno())
        if (final.st_size, final.st_mtime_ns) != (actual.st_size, actual.st_mtime_ns):
            raise ResolverFailure("FILE_CHANGED", "Asset changed during reading")


class GovernedFileResolver(GovernedResolver):
    def __init__(
        self,
        root: Path,
        assets: Sequence[FileAsset],
        *,
        limits: ResourceLimits | None = None,
        max_file_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        super().__init__(
            PluginDescriptor(
                version="nexus-plugins/v1",
                id="governed-files",
                kind="resolver",
                runtime_versions=("query-runtime/v0", "query-runtime/v1"),
                interchange=("arrow", "parquet"),
            ),
            [entry.asset for entry in assets],
            limits,
        )
        self.root = root.absolute()
        if not self.root.is_dir() or self.root.resolve() != self.root:
            raise ValueError("File root must be an existing canonical, nonsymlink directory")
        if isinstance(max_file_bytes, bool) or max_file_bytes <= 0:
            raise ValueError("File byte limit must be positive")
        self.max_file_bytes = max_file_bytes
        self._paths = {entry.asset.source.object_name: entry.relative_path for entry in assets}

    async def capabilities(self, source_alias: str) -> CapabilityCatalog:
        catalog = await super().capabilities(source_alias)
        return catalog.model_copy(
            update={
                "operator_kinds": catalog.operator_kinds - {OperatorKind.SORT},
            }
        )

    def supports_fragment(self, fragment: SourceFragment) -> bool:
        if not super().supports_fragment(fragment):
            return False
        stage = 0
        for operation in fragment.operations[1:]:
            if operation.kind is OperatorKind.FILTER and stage == 0:
                continue
            if operation.kind is OperatorKind.SELECT and stage <= 1:
                stage = 1
            elif operation.kind is OperatorKind.LIMIT:
                stage = 2
            else:
                return False
        return True

    async def _execute(
        self,
        context: ExecutionContext,
        fragment: SourceFragment,
        asset: Asset,
    ) -> pa.Table:
        validate_fragment_shape(fragment)
        stopped = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(self._scan, fragment, asset, stopped))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            stopped.set()
            await worker
            raise

    def _scan(self, fragment: SourceFragment, asset: Asset, stopped: threading.Event) -> pa.Table:
        projection = list(asset.schema.names)
        filter_columns: set[str] = set()
        predicate: ds.Expression | None = None
        limit: int | None = None
        # Push down FILTER then PROJECT then LIMIT. Reject reordering rather than change semantics.
        stage = 0
        for operation in fragment.operations[1:]:
            if operation.kind is OperatorKind.FILTER and stage == 0:
                assert operation.predicate is not None
                validate_predicate(operation.predicate.expression, asset.schema)
                pending = [operation.predicate.expression]
                while pending:
                    expression = pending.pop()
                    if expression.column is not None:
                        filter_columns.add(expression.column)
                    pending.extend(expression.args)
                rendered = _filter(operation.predicate.expression, asset.schema)
                predicate = rendered if predicate is None else predicate & rendered
            elif operation.kind is OperatorKind.SELECT and stage <= 1:
                if not set(operation.columns) <= set(projection):
                    raise ResolverFailure(
                        "FILE_COLUMN_DENIED", "Projection references unknown columns"
                    )
                projection = list(operation.columns)
                stage = 1
            elif operation.kind is OperatorKind.LIMIT:
                assert operation.limit is not None
                limit = operation.limit if limit is None else min(limit, operation.limit)
                stage = 2
            else:
                raise ResolverFailure("PUSHDOWN_UNSUPPORTED", "Unsupported file operator ordering")
        schema = pa.schema([asset.schema.field(c) for c in projection])
        path = self._paths[asset.source.object_name]
        scan_columns = [c for c in asset.schema.names if c in set(projection) | filter_columns]
        scan_schema = pa.schema([asset.schema.field(c) for c in scan_columns])
        with open_governed(self.root, path, self.max_file_bytes) as stream:
            if path.endswith(".parquet"):
                parquet = pq.ParquetFile(stream)
                if not parquet.schema_arrow.equals(asset.schema, check_metadata=False):
                    raise ResolverFailure("SOURCE_SCHEMA_MISMATCH", "Parquet schema drift detected")
                reader = parquet.iter_batches(
                    batch_size=512, columns=scan_columns, use_threads=False
                )
            else:
                reader = csv.open_csv(
                    stream,
                    read_options=csv.ReadOptions(block_size=65536, use_threads=False),
                    convert_options=csv.ConvertOptions(
                        column_types={f.name: f.type for f in asset.schema},
                        strings_can_be_null=True,
                    ),
                )
                if not reader.schema.equals(asset.schema, check_metadata=False):
                    raise ResolverFailure("SOURCE_SCHEMA_MISMATCH", "CSV schema drift detected")
                scan_schema = asset.schema
            scanner = ds.Scanner.from_batches(
                (batch for batch in reader),
                schema=scan_schema,
                columns=projection,
                filter=predicate,
                batch_size=512,
                batch_readahead=0,
                fragment_readahead=0,
                use_threads=False,
            )
            batches = []
            rows = 0
            size = 0
            if limit == 0:
                return pa.Table.from_batches([], schema=schema)
            for batch in scanner.to_batches():
                if stopped.is_set():
                    raise asyncio.CancelledError
                if limit is not None:
                    batch = batch.slice(0, max(0, limit - rows))
                rows += batch.num_rows
                size += retained_table_size(pa.Table.from_batches([batch]))
                if rows > self.limits.max_rows or size > self.limits.max_bytes:
                    raise ResourceLimitFailure("FILE_LIMIT", "Scanned output exceeds budget")
                batches.append(batch)
                if limit is not None and rows == limit:
                    break
            return pa.Table.from_batches(batches, schema=schema)


def _filter(expression: object, schema: pa.Schema) -> ds.Expression:
    from query_runtime.domain import TypedExpression

    if not isinstance(expression, TypedExpression):
        raise ResolverFailure("PUSHDOWN_INVALID", "Expected a typed predicate")
    if expression.kind is ExpressionKind.COLUMN:
        if expression.column not in schema.names:
            raise ResolverFailure("FILE_COLUMN_DENIED", "Filter references unknown columns")
        return ds.field(expression.column)
    if expression.kind is ExpressionKind.LITERAL:
        from nexus_plugins.types import scalar_arrow_type

        data_type = scalar_arrow_type(expression.data_type, expression.value)
        return ds.scalar(pa.scalar(arrow_value(expression.value, data_type), type=data_type))
    arguments = [_filter(arg, schema) for arg in expression.args]
    if expression.kind in {ExpressionKind.NOT, ExpressionKind.IS_NULL} and len(arguments) == 1:
        return ~arguments[0] if expression.kind is ExpressionKind.NOT else arguments[0].is_null()
    if len(arguments) != 2:
        raise ResolverFailure("PUSHDOWN_INVALID", "Invalid predicate arity")
    left, right = arguments
    match expression.kind:
        case ExpressionKind.EQUAL:
            return left == right
        case ExpressionKind.NOT_EQUAL:
            return left != right
        case ExpressionKind.LESS_THAN:
            return left < right
        case ExpressionKind.LESS_EQUAL:
            return left <= right
        case ExpressionKind.GREATER_THAN:
            return left > right
        case ExpressionKind.GREATER_EQUAL:
            return left >= right
        case ExpressionKind.AND:
            return left & right
        case ExpressionKind.OR:
            return left | right
        case _:
            raise ResolverFailure("PUSHDOWN_UNSUPPORTED", "File predicate is unsupported")
