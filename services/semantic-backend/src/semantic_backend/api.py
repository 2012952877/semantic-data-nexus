from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status

from semantic_backend.models import RunDetail, RunStatus, StartRunRequest
from semantic_backend.repository import (
    RunCapacityError,
    RunConflictError,
    RunNotFoundError,
)
from semantic_backend.service import OrchestrationService


def create_app(service: OrchestrationService | None = None) -> FastAPI:
    orchestrator = service or OrchestrationService()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await orchestrator.shutdown()

    app = FastAPI(
        title="Semantic Data Nexus backend",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.orchestrator = orchestrator

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        if not await orchestrator.ready():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="semantic backend is not ready",
            )
        return {"status": "ready"}

    @app.post("/v1/runs", response_model=RunStatus, status_code=status.HTTP_202_ACCEPTED)
    async def start_run(request: StartRunRequest) -> RunStatus:
        try:
            return await orchestrator.start(request)
        except RunConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except RunCapacityError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    @app.get("/v1/runs/{run_id}", response_model=RunStatus)
    async def get_run(run_id: str) -> RunStatus:
        try:
            return await orchestrator.get_status(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
            ) from exc

    @app.post("/v1/runs/{run_id}/cancel", response_model=RunStatus)
    async def cancel_run(run_id: str) -> RunStatus:
        try:
            return await orchestrator.cancel(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
            ) from exc

    @app.get("/v1/runs/{run_id}/detail", response_model=RunDetail)
    async def get_detail(run_id: str) -> RunDetail:
        try:
            return await orchestrator.get_detail(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
            ) from exc

    @app.get("/v1/runs/{run_id}/result", response_model=RunDetail)
    async def get_result(run_id: str) -> RunDetail:
        return await get_detail(run_id)

    @app.get("/v1/runs/{run_id}/lineage", response_model=RunDetail)
    async def get_lineage(run_id: str) -> RunDetail:
        return await get_detail(run_id)

    return app
