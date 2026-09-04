from __future__ import annotations

import asyncio

from conftest import request_for, wait_for_terminal
from httpx import ASGITransport, AsyncClient
from query_runtime.coordinator import QueryCoordinator

from semantic_backend.api import create_app
from semantic_backend.models import RunState


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

        for _ in range(200):
            response = await client.get(f"/v1/runs/{request.run_id}")
            if response.json()["state"] in {"Succeeded", "Failed", "Cancelled"}:
                break
            await asyncio.sleep(0.01)
        assert response.json()["state"] == "Succeeded"

        detail = await client.get(f"/v1/runs/{request.run_id}/detail")
        assert detail.status_code == 200
        assert detail.json()["manifest"]["rowCount"] == 4


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
