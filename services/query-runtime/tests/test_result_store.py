from __future__ import annotations

import asyncio
from pathlib import Path

import pyarrow as pa
import pytest

from query_runtime.errors import ResultStoreFailure
from query_runtime.result_store import (
    HybridResultStore,
    InlineResultStore,
    ParquetResultStore,
)


@pytest.mark.asyncio
async def test_inline_and_parquet_paging(tmp_path: Path) -> None:
    table = pa.table({"value": list(range(20))})
    inline = InlineResultStore()
    inline_manifest = await inline.commit("run-inline", "node-inline", table)
    assert (await inline.read_page(inline_manifest.result, 5, 3)).to_pylist() == [
        {"value": 5},
        {"value": 6},
        {"value": 7},
    ]

    hybrid = HybridResultStore(tmp_path, inline_max_bytes=1)
    manifest = await hybrid.commit("run-parquet", "node-parquet", table)
    assert manifest.result.storage == "parquet"
    assert (Path(manifest.result.uri) / "_COMMITTED").is_file()
    assert (await hybrid.read_page(manifest.result, 18, 10)).num_rows == 2


class _FailingStore(ParquetResultStore):
    def _commit_directory(self, temporary: Path, final: Path) -> None:
        raise OSError("synthetic commit failure")


@pytest.mark.asyncio
async def test_failed_output_is_never_committed(tmp_path: Path) -> None:
    store = _FailingStore(tmp_path)
    with pytest.raises(ResultStoreFailure) as error:
        await store.commit("run-fail", "node-fail", pa.table({"x": [1]}))
    assert error.value.code == "RESULT_COMMIT_FAILED"
    assert not list(tmp_path.rglob("_COMMITTED"))  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_traversal_and_unbounded_pages_are_rejected(tmp_path: Path) -> None:
    store = ParquetResultStore(tmp_path)
    with pytest.raises(ResultStoreFailure) as traversal:
        await store.commit("../escape", "node", pa.table({"x": [1]}))
    assert traversal.value.code == "RESULT_PATH_INVALID"
    manifest = await store.commit("run", "node", pa.table({"x": [1]}))
    with pytest.raises(ResultStoreFailure) as page:
        await store.read_page(manifest.result, 0, 10_001)
    assert page.value.code == "RESULT_PAGE_INVALID"


@pytest.mark.asyncio
async def test_cancelled_commit_cannot_publish(tmp_path: Path) -> None:
    store = ParquetResultStore(tmp_path)
    cancelled = asyncio.Event()
    cancelled.set()
    with pytest.raises(asyncio.CancelledError):
        await store.commit(
            "run-cancelled", "node-cancelled", pa.table({"x": [1]}), cancelled
        )
    assert not list(tmp_path.rglob("_COMMITTED"))  # noqa: ASYNC240
