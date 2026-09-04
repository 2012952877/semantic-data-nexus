"""Arrow retained-buffer accounting helpers."""

from __future__ import annotations

from collections.abc import Mapping

import pyarrow as pa

BufferIdentity = tuple[int, int]


def retained_buffer_sizes(table: pa.Table) -> Mapping[BufferIdentity, int]:
    """Return each physical Arrow buffer retained by a table exactly once."""
    buffers: dict[BufferIdentity, int] = {}
    for column in table.columns:
        for chunk in column.chunks:
            _collect_array_buffers(chunk, buffers)
    return buffers


def retained_table_size(table: pa.Table) -> int:
    return sum(retained_buffer_sizes(table).values())


def _collect_array_buffers(
    array: pa.Array, buffers: dict[BufferIdentity, int]
) -> None:
    for buffer in array.buffers():
        if buffer is None:
            continue
        root = buffer
        while root.parent is not None:
            root = root.parent
        if root.size:
            buffers[(root.address, root.size)] = root.size
    if isinstance(array, pa.DictionaryArray):
        _collect_array_buffers(array.dictionary, buffers)
    elif isinstance(array, pa.ExtensionArray):
        _collect_array_buffers(array.storage, buffers)
    elif isinstance(array, pa.StructArray):
        for index in range(array.type.num_fields):
            _collect_array_buffers(array.field(index), buffers)
    elif isinstance(
        array,
        (
            pa.ListArray,
            pa.LargeListArray,
            pa.ListViewArray,
            pa.LargeListViewArray,
            pa.FixedSizeListArray,
            pa.MapArray,
        ),
    ):
        _collect_array_buffers(array.values, buffers)
    elif isinstance(array, pa.UnionArray):
        for index in range(array.type.num_fields):
            _collect_array_buffers(array.field(index), buffers)
    elif isinstance(array, pa.RunEndEncodedArray):
        _collect_array_buffers(array.run_ends, buffers)
        _collect_array_buffers(array.values, buffers)
