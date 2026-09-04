from __future__ import annotations

import asyncio
import json
from pathlib import Path

from conftest import request_for, wait_for_terminal
from httpx import ASGITransport, AsyncClient
from query_runtime.coordinator import QueryCoordinator
from query_runtime.domain import ExecutionState
from query_runtime.events import InMemoryEventStore

from semantic_backend.api import create_app
from semantic_backend.models import RunState

CONTRACT_FIXTURES = Path(__file__).parents[1] / "contract-fixtures" / "v1"


async def test_health_start_status_detail_and_idempotency(service) -> None:
    app = create_app(service)
    transport = ASGITransport(app=app)
    request = request_for("run_00000000000000000000000000000003")
    payload = request.model_dump(mode="json", by_alias=True)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200
        first = await client.post("/v1/runs", json=payload)
        second = await client.post("/v1/runs", json=payload)
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["runId"] == request.run_id

        for _ in range(1_000):
            response = await client.get(f"/v1/runs/{request.run_id}")
            if response.json()["state"] in {"Succeeded", "Failed", "Cancelled"}:
                break
            await asyncio.sleep(0.01)
        assert response.json()["state"] == "Succeeded"

        detail = await client.get(f"/v1/runs/{request.run_id}/detail")
        assert detail.status_code == 200
        assert detail.json()["manifest"]["rowCount"] == 4


async def test_real_bff_fixture_starts_through_http_boundary(service) -> None:
    app = create_app(service)
    transport = ASGITransport(app=app)
    payload = json.loads((CONTRACT_FIXTURES / "bff-start-request.json").read_text(encoding="utf-8"))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/runs", json=payload)
        assert response.status_code == 202
        assert response.json()["runId"] == payload["runId"]
        detail = await client.get(f"/v1/runs/{payload['runId']}/detail")
        assert detail.status_code == 200
        assert detail.json()["question"] == payload["question"]
        assert (await client.post(f"/v1/runs/{payload['runId']}/cancel")).status_code == 200


async def test_long_question_keeps_full_detail_and_bounded_sqg_intent(service) -> None:
    question = "上季度各区域利润是多少?" + ("a" * 600)
    request = request_for("run_00000000000000000000000000000103").model_copy(
        update={"question": question}
    )
    started = await service.start(request)
    detail = await service.get_detail(request.run_id)
    assert started.state in {RunState.STARTING, RunState.RUNNING}
    assert detail.question == question
    assert detail.sqg.intent == question[:512]
    await service.cancel(request.run_id)


async def test_conflicting_id_and_validation_fail_closed(service) -> None:
    app = create_app(service)
    transport = ASGITransport(app=app)
    request = request_for("run_00000000000000000000000000000004")
    payload = request.model_dump(mode="json", by_alias=True)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/v1/runs", json=payload)).status_code == 202
        conflict = dict(payload)
        conflict["question"] = "另一个安全合成问题"
        assert (await client.post("/v1/runs", json=conflict)).status_code == 409

        invalid = dict(payload)
        invalid["runId"] = "not-canonical"
        invalid["unexpected"] = True
        assert (await client.post("/v1/runs", json=invalid)).status_code == 422
        await wait_for_terminal(service, request.run_id)


async def test_cancellation_is_idempotent(service) -> None:
    request = request_for("run_00000000000000000000000000000005")
    await service.start(request)
    first = await service.cancel(request.run_id)
    second = await service.cancel(request.run_id)
    assert first.state is RunState.CANCELLED
    assert second.state is RunState.CANCELLED
    assert second.finalized_at is not None


async def test_cancellation_before_runtime_registration_cannot_publish(
    service,
    monkeypatch,
) -> None:
    entered = asyncio.Event()

    async def delayed_run(self, plan, *, run_id=None):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(QueryCoordinator, "run", delayed_run)
    request = request_for("run_00000000000000000000000000000007")
    await service.start(request)
    await asyncio.wait_for(entered.wait(), timeout=2)
    status = await service.cancel(request.run_id)
    detail = await service.get_detail(request.run_id)
    assert status.state is RunState.CANCELLED
    assert detail.result is None
    assert detail.manifest is None


async def test_shutdown_never_waits_for_current_task(service) -> None:
    request = request_for("run_00000000000000000000000000000010")
    await service.start(request)
    await wait_for_terminal(service, request.run_id)
    record = await service.repository.get(request.run_id)
    original_status = record.status
    original_task = record.task
    record.status = record.status.model_copy(
        update={"state": RunState.RUNNING, "finalized_at": None}
    )
    record.task = asyncio.current_task()
    try:
        await asyncio.wait_for(service.shutdown(), timeout=1)
    finally:
        record.status = original_status
        record.task = original_task


async def test_late_cancellation_preserves_completed_coordinator_outcomes(
    service,
    monkeypatch,
) -> None:
    completed = asyncio.Event()
    release = asyncio.Event()
    original_run = QueryCoordinator.run

    async def delayed_return(self, plan, *, run_id=None):
        outcome = await original_run(self, plan, run_id=run_id)
        completed.set()
        await release.wait()
        return outcome

    monkeypatch.setattr(QueryCoordinator, "run", delayed_return)
    request = request_for("run_00000000000000000000000000000011")
    await service.start(request)
    await asyncio.wait_for(completed.wait(), timeout=3)
    cancellation = asyncio.create_task(service.cancel(request.run_id))
    await asyncio.sleep(0)
    assert not cancellation.done()
    release.set()
    status = await cancellation
    assert status.state is RunState.SUCCEEDED
    execute = next(stage for stage in status.stages if stage.name == "Execute")
    assert all(node.state is RunState.SUCCEEDED for node in execute.nodes)


async def test_cancellation_during_terminal_event_persistence_keeps_success(
    service,
    monkeypatch,
) -> None:
    terminal_append = asyncio.Event()
    release = asyncio.Event()
    original_append = InMemoryEventStore.append

    async def delayed_terminal_append(self, event):
        if event.scope == "run" and event.state is ExecutionState.SUCCEEDED:
            terminal_append.set()
            await release.wait()
        await original_append(self, event)

    monkeypatch.setattr(InMemoryEventStore, "append", delayed_terminal_append)
    request = request_for("run_00000000000000000000000000000012")
    await service.start(request)
    await asyncio.wait_for(terminal_append.wait(), timeout=3)
    cancellation = asyncio.create_task(service.cancel(request.run_id))
    await asyncio.sleep(0)
    assert not cancellation.done()
    release.set()
    status = await cancellation
    assert status.state is RunState.SUCCEEDED
    assert (await service.get_detail(request.run_id)).result is not None
