from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
from query_runtime.domain import BoundSource, OperatorKind, OperatorSpec, SourceFragment
from query_runtime.resolver import ExecutionContext

from nexus_plugins.contracts import Asset, CredentialReference


@dataclass(repr=False)
class SyntheticCredentials:
    values: Mapping[str, str] = field(repr=False)

    async def resolve(self, reference: CredentialReference) -> Mapping[str, str]:
        assert reference.id.startswith("credential:synthetic-")
        return dict(self.values)


def asset(
    source_type: str = "governed-files",
    *,
    schema: pa.Schema | None = None,
    alias: str = "orders",
    table: tuple[str, ...] = (),
) -> Asset:
    return Asset(
        BoundSource(alias=alias, source_type=source_type, object_name=alias),
        schema if schema is not None else pa.schema([("id", pa.int64()), ("name", pa.string())]),
        table,
    )


def fragment(item: Asset, *operations: OperatorSpec) -> SourceFragment:
    return SourceFragment(
        source=item.source,
        operations=(
            OperatorSpec(kind=OperatorKind.SOURCE),
            *operations,
        ),
    )


def context(handle: str = "synthetic-handle") -> ExecutionContext:
    return ExecutionContext("synthetic-run", "source", 1, handle)


def statement_response(
    columns: list[tuple[str, str]],
    rows: list[list[Any]],
    *,
    statement_id: str = "synthetic-statement",
) -> dict[str, object]:
    return {
        "statement_id": statement_id,
        "status": {"state": "SUCCEEDED"},
        "manifest": {
            "format": "JSON_ARRAY",
            "truncated": False,
            "schema": {
                "columns": [
                    {"name": name, "position": index, "type_name": kind, "type_text": kind}
                    for index, (name, kind) in enumerate(columns)
                ]
            },
        },
        "result": {"data_array": rows},
    }
