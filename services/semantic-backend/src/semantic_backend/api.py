from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from psycopg import Error as PostgresError
from query_runtime.errors import RuntimeFailure
from semantic_api.catalog_v1.models import CatalogCompileRequest, CompilerFailure

from semantic_backend.auth_context import AccessDenied, get_trusted_context, legacy_development
from semantic_backend.auth_middleware import ServiceAuthentication
from semantic_backend.authorization import PostgresAuthorization
from semantic_backend.catalog_disconnect import DISCONNECT_KEY, CatalogDisconnect, run_connected
from semantic_backend.catalog_models import (
    CatalogAnswerRequest,
    CatalogAnswerResponse,
    CatalogQueryResponse,
)
from semantic_backend.catalog_service import CatalogQueryService
from semantic_backend.models import RunDetail, RunStatus, StartRunRequest
from semantic_backend.repository import (
    RunCapacityError,
    RunConflictError,
    RunNotFoundError,
)
from semantic_backend.service import OrchestrationService


def create_app(
    service: OrchestrationService | None = None,
    catalog_service: CatalogQueryService | None = None,
) -> FastAPI:
    orchestrator = service or OrchestrationService()
    legacy = legacy_development()
    if catalog_service is None and isinstance(orchestrator.authority, PostgresAuthorization):
        catalog_service = CatalogQueryService.from_environment(orchestrator.authority)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            if catalog_service is not None:
                await catalog_service.initialize()
            yield
        finally:
            if catalog_service is not None:
                await catalog_service.shutdown()
            await orchestrator.shutdown()

    app = FastAPI(
        title="Semantic Data Nexus backend",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.orchestrator = orchestrator
    app.state.catalog_service = catalog_service
    if not legacy:
        authority = orchestrator.authority
        if not isinstance(authority, PostgresAuthorization):
            raise ValueError("The HTTP service requires the shared PostgreSQL authority")
        # Validate key/configuration at construction, not on the first user's request.
        ServiceAuthentication(app, authority=authority)
        app.add_middleware(ServiceAuthentication, authority=authority)
    app.add_middleware(CatalogDisconnect)

    @app.exception_handler(AccessDenied)
    async def denied(_request: object, _exception: AccessDenied) -> JSONResponse:
        return JSONResponse({"detail": "Service authorization denied"}, status_code=403)

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
        if not legacy and request.requested_by != get_trusted_context().principal.principal_id:
            raise AccessDenied("Requested principal does not match the authenticated context.")
        try:
            return await orchestrator.start(
                request, context=None if legacy else get_trusted_context()
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
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
            return await orchestrator.get_status(
                run_id, context=None if legacy else get_trusted_context()
            )
        except RunNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
            ) from exc

    @app.post("/v1/runs/{run_id}/cancel", response_model=RunStatus)
    async def cancel_run(run_id: str) -> RunStatus:
        try:
            return await orchestrator.cancel(
                run_id, context=None if legacy else get_trusted_context()
            )
        except RunNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
            ) from exc

    @app.get("/v1/runs/{run_id}/detail", response_model=RunDetail)
    async def get_detail(run_id: str) -> RunDetail:
        try:
            return await orchestrator.get_detail(
                run_id, context=None if legacy else get_trusted_context()
            )
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

    async def catalog_call[T](operation: Awaitable[T]) -> T:
        try:
            return await operation
        except CompilerFailure as exc:
            code = exc.code
            status_code = (
                404
                if code in {"RESOURCE_NOT_AVAILABLE", "CLARIFICATION_NOT_AVAILABLE"}
                else 403
                if code in {"CLARIFICATION_CONTEXT_CHANGED", "AUTHORIZATION_CHANGED"}
                else 410
                if code == "CLARIFICATION_EXPIRED"
                else 409
                if code
                in {
                    "IDEMPOTENCY_CONFLICT",
                    "CLARIFICATION_REQUEST_MISMATCH",
                    "ATTEMPT_FENCED",
                    "COMPILATION_IN_PROGRESS",
                    "COMPILATION_OUTCOME_UNKNOWN",
                    "CLARIFICATION_REVISION",
                    "RUNTIME_IN_PROGRESS",
                    "RUNTIME_OUTCOME_UNKNOWN",
                    "RUNTIME_FENCED",
                    "RUNTIME_POLICY_CHANGED",
                    "COMPILER_CONFIGURATION_CHANGED",
                }
                else 422
            )
            raise HTTPException(status_code=status_code, detail={"code": code}) from None
        except TimeoutError:
            raise HTTPException(status_code=504, detail={"code": "CATALOG_TIMEOUT"}) from None
        except PostgresError:
            raise HTTPException(
                status_code=503, detail={"code": "CATALOG_STORE_UNAVAILABLE"}
            ) from None
        except RuntimeFailure:
            raise HTTPException(
                status_code=422, detail={"code": "CATALOG_RUNTIME_FAILED"}
            ) from None

    def enabled_catalog() -> CatalogQueryService:
        get_trusted_context()
        if legacy or catalog_service is None:
            raise HTTPException(status_code=503, detail={"code": "CATALOG_NOT_CONFIGURED"})
        return catalog_service

    @app.post("/v1/catalog/queries", response_model=CatalogQueryResponse)
    async def catalog_query(
        request: CatalogCompileRequest, http: Request
    ) -> CatalogQueryResponse | Response:
        catalog = enabled_catalog()
        return await run_connected(
            catalog_call(catalog.query(request, get_trusted_context())), http.scope[DISCONNECT_KEY]
        )

    @app.post(
        "/v1/catalog/clarifications/{identifier}/answers", response_model=CatalogAnswerResponse
    )
    async def catalog_answer(
        identifier: str, request: CatalogAnswerRequest, http: Request
    ) -> CatalogAnswerResponse | Response:
        catalog = enabled_catalog()
        return await run_connected(
            catalog_call(catalog.answer(identifier, request, get_trusted_context())),
            http.scope[DISCONNECT_KEY],
        )

    return app
