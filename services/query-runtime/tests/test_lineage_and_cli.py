from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pyarrow as pa
import pytest

from query_runtime.cli import execute
from query_runtime.coordinator import QueryCoordinator
from query_runtime.domain import (
    BoundParameter,
    BoundSource,
    CapabilityCatalog,
    DiagnosticEvent,
    ExecutionState,
    OperatorKind,
    OperatorSpec,
    PhysicalNode,
    PhysicalNodeKind,
    PhysicalPlan,
    ScalarType,
    SourceFragment,
)
from query_runtime.events import InMemoryEventStore
from query_runtime.resolver import FakeResolver
from query_runtime.result_store import ParquetResultStore


@pytest.mark.asyncio
async def test_lineage_is_complete_and_redacts_parameter_values(tmp_path: Path) -> None:
    alias = "safe_source"
    node = PhysicalNode(
        id="safe-node",
        kind=PhysicalNodeKind.SOURCE_FRAGMENT,
        operation=OperatorKind.SOURCE,
        wave=0,
        logical_node_ids=("logical-safe",),
        source_fragment=SourceFragment(
            source=BoundSource(
                alias=alias, source_type="synthetic", object_name="safe_facts"
            ),
            operations=(OperatorSpec(kind=OperatorKind.SOURCE),),
            parameters=(
                BoundParameter(
                    name="region_key",
                    data_type=ScalarType.STRING,
                    value="DO-NOT-RECORD-SECRET",
                ),
            ),
        ),
    )
    plan = PhysicalPlan(id="lineage", nodes=(node,), output_node_id=node.id)
    resolver = FakeResolver(
        tables={alias: pa.table({"value": [1]})},
        catalogs={
            alias: CapabilityCatalog(
                source_alias=alias,
                source_type="synthetic",
                operator_kinds=frozenset({OperatorKind.SOURCE}),
            )
        },
    )
    outcome = await QueryCoordinator(
        resolver=resolver, result_store=ParquetResultStore(tmp_path)
    ).run(plan, run_id="run-lineage")
    payload = outcome.lineage.model_dump_json()
    assert "DO-NOT-RECORD-SECRET" not in payload
    assert json.loads(payload)["nodes"]
    relations = {edge.relation for edge in outcome.lineage.edges}
    assert {"realized_as", "reads_from", "produces"} <= relations


@pytest.mark.asyncio
async def test_cli_smoke_creates_parquet_manifest(tmp_path: Path) -> None:
    payload = await execute("complex", tmp_path)
    assert payload["summary"]["state"] == "SUCCEEDED"  # type: ignore[index]
    manifest = payload["manifest"]
    assert isinstance(manifest, dict)
    assert manifest["result"]["storage"] == "parquet"
    assert Path(manifest["result"]["uri"], "_COMMITTED").is_file()  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_slow_event_subscriber_cannot_abort_publish() -> None:
    store = InMemoryEventStore()
    subscriber = store.subscribe("run-slow")
    first = asyncio.create_task(anext(subscriber))
    await asyncio.sleep(0)
    await store.append(
        DiagnosticEvent(
            sequence=0,
            run_id="run-slow",
            scope="run",
            scope_id="run-slow",
            state=ExecutionState.RUNNING,
            code="RUNNING",
            message="running",
        )
    )
    await first
    for sequence in range(1, 400):
        await store.append(
            DiagnosticEvent(
                sequence=sequence,
                run_id="run-slow",
                scope="run",
                scope_id="run-slow",
                state=ExecutionState.RUNNING,
                code="RUNNING",
                message="running",
            )
        )
    assert len(await store.list("run-slow")) == 400
    await subscriber.aclose()
