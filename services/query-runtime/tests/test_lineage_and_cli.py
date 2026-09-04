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
from query_runtime.resolver import AdapterResolver, ExecutionContext, FakeResolver
from query_runtime.result_store import ParquetResultStore


class _RecordingParameterizedAdapter:
    def __init__(self, table: pa.Table) -> None:
        self.table = table
        self.contexts: list[ExecutionContext] = []
        self.cancelled: list[str] = []

    async def execute_validated_fragment(
        self,
        context: ExecutionContext,
        fragment: SourceFragment,
        cancel_event: asyncio.Event,
    ) -> pa.Table:
        self.contexts.append(context)
        return self.table

    async def cancel(self, cancellation_handle: str) -> None:
        self.cancelled.append(cancellation_handle)

    async def health(self) -> bool:
        return True

    async def capabilities(self, source_alias: str) -> CapabilityCatalog:
        return CapabilityCatalog(
            source_alias=source_alias,
            source_type="synthetic",
            operator_kinds=frozenset({OperatorKind.SOURCE}),
        )


@pytest.mark.asyncio
async def test_adapter_resolver_forwards_execution_and_cancellation_identity() -> None:
    adapter = _RecordingParameterizedAdapter(pa.table({"value": [1]}))
    resolver = AdapterResolver(adapter)
    context = ExecutionContext(
        run_id="run-adapter",
        node_id="node-adapter",
        attempt=1,
        cancellation_handle="exec-adapter",
    )
    fragment = SourceFragment(
        source=BoundSource(
            alias="adapter-source",
            source_type="synthetic",
            object_name="facts",
        ),
        operations=(OperatorSpec(kind=OperatorKind.SOURCE),),
    )
    table = await resolver.execute(context, fragment, asyncio.Event())
    await resolver.cancel(context.cancellation_handle)
    assert table.to_pylist() == [{"value": 1}]
    assert adapter.contexts == [context]
    assert adapter.cancelled == ["exec-adapter"]


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
    with pytest.raises(StopAsyncIteration):
        await anext(subscriber)


@pytest.mark.asyncio
async def test_event_subscription_closes_at_terminal_run() -> None:
    store = InMemoryEventStore()
    subscriber = store.subscribe("run-terminal")
    waiting = asyncio.create_task(anext(subscriber))
    await asyncio.sleep(0)
    terminal = DiagnosticEvent(
        sequence=0,
        run_id="run-terminal",
        scope="run",
        scope_id="run-terminal",
        state=ExecutionState.SUCCEEDED,
        code="RUN_SUCCEEDED",
        message="done",
    )
    await store.append(terminal)
    assert await waiting == terminal
    with pytest.raises(StopAsyncIteration):
        await anext(subscriber)
    late = store.subscribe("run-terminal")
    with pytest.raises(StopAsyncIteration):
        await anext(late)


@pytest.mark.asyncio
async def test_terminal_close_preserves_full_subscriber_queue() -> None:
    store = InMemoryEventStore()
    subscriber = store.subscribe("run-full-terminal")
    waiting = asyncio.create_task(anext(subscriber))
    await asyncio.sleep(0)
    first = DiagnosticEvent(
        sequence=0,
        run_id="run-full-terminal",
        scope="run",
        scope_id="run-full-terminal",
        state=ExecutionState.RUNNING,
        code="RUNNING",
        message="running",
    )
    await store.append(first)
    assert await waiting == first
    for sequence in range(1, 256):
        await store.append(
            first.model_copy(update={"sequence": sequence})
        )
    terminal = first.model_copy(
        update={
            "sequence": 256,
            "state": ExecutionState.SUCCEEDED,
            "code": "RUN_SUCCEEDED",
            "message": "done",
        }
    )
    await store.append(terminal)
    drained = [await anext(subscriber) for _ in range(256)]
    assert [event.sequence for event in drained] == list(range(1, 257))
    with pytest.raises(StopAsyncIteration):
        await anext(subscriber)
