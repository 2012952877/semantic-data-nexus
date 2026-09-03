"""Atomic inline and filesystem Parquet result stores."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from query_runtime.domain import (
    CommittedManifest,
    ResultHandle,
    ResultPart,
    SchemaField,
    TableSchema,
    utc_now,
)
from query_runtime.errors import ResultStoreFailure

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ResultStore(Protocol):
    async def commit(
        self,
        run_id: str,
        node_id: str,
        table: pa.Table,
        cancel_event: asyncio.Event | None = None,
    ) -> CommittedManifest: ...

    async def read_page(
        self, handle: ResultHandle, offset: int, limit: int
    ) -> pa.Table: ...


class AzureBlobResultAdapter(Protocol):
    """SDK-free boundary for a future Azure Blob implementation."""

    async def upload_part(
        self, run_id: str, node_id: str, part_name: str, data: bytes
    ) -> str: ...

    async def publish_manifest(
        self, run_id: str, node_id: str, manifest: CommittedManifest
    ) -> ResultHandle: ...

    async def read_page(
        self, handle: ResultHandle, offset: int, limit: int
    ) -> pa.Table: ...


def arrow_schema(schema: pa.Schema) -> TableSchema:
    return TableSchema(
        fields=tuple(
            SchemaField(
                name=field.name,
                data_type=str(field.type),
                nullable=field.nullable,
            )
            for field in schema
        )
    )


def validate_id(value: str, kind: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ResultStoreFailure(
            "RESULT_PATH_INVALID",
            f"{kind} contains characters that are unsafe for result storage",
        )
    return value


class InlineResultStore:
    def __init__(self) -> None:
        self._tables: dict[str, pa.Table] = {}
        self._manifests: dict[str, CommittedManifest] = {}

    async def commit(
        self,
        run_id: str,
        node_id: str,
        table: pa.Table,
        cancel_event: asyncio.Event | None = None,
    ) -> CommittedManifest:
        validate_id(run_id, "run_id")
        validate_id(node_id, "node_id")
        result_id = uuid.uuid4().hex
        uri = f"inline://{run_id}/{node_id}/{result_id}"
        handle = ResultHandle(
            result_id=result_id,
            run_id=run_id,
            node_id=node_id,
            storage="inline",
            uri=uri,
        )
        manifest = CommittedManifest(
            result=handle,
            schema=arrow_schema(table.schema),
            row_count=table.num_rows,
            byte_count=table.nbytes,
            parts=(ResultPart(path=uri, rows=table.num_rows, bytes=table.nbytes),),
            committed_at=utc_now(),
        )
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        self._tables[result_id] = table
        self._manifests[result_id] = manifest
        return manifest

    async def read_page(
        self, handle: ResultHandle, offset: int, limit: int
    ) -> pa.Table:
        _validate_page(offset, limit)
        if handle.storage != "inline":
            raise ResultStoreFailure("RESULT_HANDLE_INVALID", "Expected an inline handle")
        manifest = self._manifests.get(handle.result_id)
        if manifest is None:
            raise ResultStoreFailure("RESULT_NOT_COMMITTED", "Inline result is not committed")
        if handle != manifest.result:
            raise ResultStoreFailure(
                "RESULT_HANDLE_INVALID",
                "Inline result handle does not match the committed manifest",
            )
        table = self._tables.get(handle.result_id)
        if table is None:
            raise ResultStoreFailure("RESULT_NOT_COMMITTED", "Inline result is not committed")
        return table.slice(offset, limit)


class ParquetResultStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def commit(
        self,
        run_id: str,
        node_id: str,
        table: pa.Table,
        cancel_event: asyncio.Event | None = None,
    ) -> CommittedManifest:
        validate_id(run_id, "run_id")
        validate_id(node_id, "node_id")
        result_id = uuid.uuid4().hex
        run_root = self._safe_child(run_id, node_id)
        run_root.mkdir(parents=True, exist_ok=True)
        temporary = run_root / f".tmp-{result_id}"
        final = run_root / result_id
        temporary.mkdir()
        write_task = asyncio.create_task(
            asyncio.to_thread(
                self._write_temporary,
                temporary,
                final,
                result_id,
                run_id,
                node_id,
                table,
            )
        )
        try:
            manifest = await asyncio.shield(write_task)
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            (temporary / "_COMMITTED").write_text("", encoding="ascii")  # noqa: ASYNC240
            self._commit_directory(temporary, final)
            return manifest
        except asyncio.CancelledError:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(write_task)
            if temporary.exists():  # noqa: ASYNC240
                shutil.rmtree(temporary)
            raise
        except Exception as exc:
            if temporary.exists():  # noqa: ASYNC240
                shutil.rmtree(temporary)
            if isinstance(exc, ResultStoreFailure):
                raise
            raise ResultStoreFailure(
                "RESULT_COMMIT_FAILED", "Result could not be committed atomically"
            ) from exc

    def _write_temporary(
        self,
        temporary: Path,
        final: Path,
        result_id: str,
        run_id: str,
        node_id: str,
        table: pa.Table,
    ) -> CommittedManifest:
        part = temporary / "part-00000.parquet"
        pq.write_table(table, part, row_group_size=64_000)
        size = part.stat().st_size
        handle = ResultHandle(
            result_id=result_id,
            run_id=run_id,
            node_id=node_id,
            storage="parquet",
            uri=str(final),
        )
        manifest = CommittedManifest(
            result=handle,
            schema=arrow_schema(table.schema),
            row_count=table.num_rows,
            byte_count=size,
            parts=(ResultPart(path=part.name, rows=table.num_rows, bytes=size),),
            committed_at=utc_now(),
        )
        (temporary / "manifest.json").write_text(
            manifest.model_dump_json(by_alias=True, indent=2), encoding="utf-8"
        )
        return manifest

    def _commit_directory(self, temporary: Path, final: Path) -> None:
        os.replace(temporary, final)

    async def read_page(
        self, handle: ResultHandle, offset: int, limit: int
    ) -> pa.Table:
        _validate_page(offset, limit)
        if handle.storage != "parquet":
            raise ResultStoreFailure("RESULT_HANDLE_INVALID", "Expected a Parquet handle")
        validate_id(handle.run_id, "run_id")
        validate_id(handle.node_id, "node_id")
        validate_id(handle.result_id, "result_id")
        result_path = self._safe_child(handle.run_id, handle.node_id, handle.result_id)
        if str(result_path) != handle.uri:
            raise ResultStoreFailure(
                "RESULT_HANDLE_INVALID", "Result handle URI does not match its isolated path"
            )
        marker = result_path / "_COMMITTED"
        manifest_path = result_path / "manifest.json"
        if not marker.is_file() or not manifest_path.is_file():
            raise ResultStoreFailure("RESULT_NOT_COMMITTED", "Result is not committed")
        manifest = CommittedManifest.model_validate(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
        return await asyncio.to_thread(
            self._read_page_sync, result_path, manifest, offset, limit
        )

    @staticmethod
    def _read_page_sync(
        result_path: Path,
        manifest: CommittedManifest,
        offset: int,
        limit: int,
    ) -> pa.Table:
        remaining = limit
        skip = offset
        chunks: list[pa.Table] = []
        result_schema: pa.Schema | None = None
        for part in manifest.parts:
            if remaining == 0:
                break
            parquet = pq.ParquetFile(result_path / part.path)
            result_schema = result_schema or parquet.schema_arrow
            if skip >= part.rows:
                skip -= part.rows
                continue
            for row_group_index in range(parquet.num_row_groups):
                row_count = parquet.metadata.row_group(row_group_index).num_rows
                if skip >= row_count:
                    skip -= row_count
                    continue
                row_group = parquet.read_row_group(row_group_index)
                take = min(remaining, row_count - skip)
                chunks.append(row_group.slice(skip, take))
                remaining -= take
                skip = 0
                if remaining == 0:
                    break
        if chunks:
            return pa.concat_tables(chunks)
        return pa.Table.from_batches([], schema=result_schema or pa.schema([]))

    def _safe_child(self, *parts: str) -> Path:
        for part in parts:
            validate_id(part, "path component")
        candidate = self.root.joinpath(*parts).resolve()
        if self.root != candidate and self.root not in candidate.parents:
            raise ResultStoreFailure(
                "RESULT_PATH_TRAVERSAL", "Result path escapes the configured root"
            )
        return candidate


class HybridResultStore:
    def __init__(self, root: Path, inline_max_bytes: int = 32 * 1024) -> None:
        self.inline_max_bytes = inline_max_bytes
        self.inline = InlineResultStore()
        self.parquet = ParquetResultStore(root)

    async def commit(
        self,
        run_id: str,
        node_id: str,
        table: pa.Table,
        cancel_event: asyncio.Event | None = None,
    ) -> CommittedManifest:
        store: ResultStore = (
            self.inline if table.nbytes <= self.inline_max_bytes else self.parquet
        )
        return await store.commit(run_id, node_id, table, cancel_event)

    async def read_page(
        self, handle: ResultHandle, offset: int, limit: int
    ) -> pa.Table:
        store: ResultStore = self.inline if handle.storage == "inline" else self.parquet
        return await store.read_page(handle, offset, limit)


def _validate_page(offset: int, limit: int) -> None:
    if offset < 0 or limit <= 0 or limit > 10_000:
        raise ResultStoreFailure(
            "RESULT_PAGE_INVALID",
            "Page offset must be non-negative and limit must be between 1 and 10000",
        )
