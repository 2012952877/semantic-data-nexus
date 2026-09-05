from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from semantic_api.compiler import SemanticCompiler
from semantic_api.models import (
    CompileRequest,
    CompileResponse,
    InitializeRequest,
    InitializeResponse,
    ProblemDetails,
)

logger = logging.getLogger("semantic_api")


def create_app(compiler: SemanticCompiler | None = None) -> FastAPI:
    service = compiler or SemanticCompiler.default()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await service.aclose()

    app = FastAPI(
        title="Semantic Compiler API",
        version="1.0.0",
        lifespan=lifespan,
        description=(
            "Initializes governed semantic context and compiles validated typed SQG. "
            "This service does not generate or execute SQL."
        ),
    )

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Any]],
    ) -> Any:
        request_id = request.headers.get("x-request-id") or str(uuid4())
        request.state.request_id = request_id[:128]
        try:
            response = await call_next(request)
        except Exception as error:
            logger.error(
                "request_failed",
                extra={
                    "request_id": request.state.request_id,
                    "path": request.url.path,
                    "error_type": type(error).__name__,
                },
            )
            problem = ProblemDetails(
                type="https://semantic-data-nexus.example/problems/internal",
                title="Internal service error",
                status=500,
                detail="The request could not be completed.",
                instance=request.url.path,
                request_id=request.state.request_id,
            )
            response = JSONResponse(
                status_code=500,
                content=problem.model_dump(mode="json"),
            )
        response.headers["x-request-id"] = request.state.request_id
        return response

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        problem = ProblemDetails(
            type="https://semantic-data-nexus.example/problems/request-validation",
            title="Request validation failed",
            status=422,
            detail="The request does not match the v1 API schema.",
            instance=request.url.path,
            request_id=_request_id(request),
        )
        logger.info(
            "request_validation_failed",
            extra={
                "request_id": problem.request_id,
                "path": request.url.path,
                "error_count": len(error.errors()),
            },
        )
        return JSONResponse(status_code=422, content=problem.model_dump(mode="json"))

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def health_ready() -> JSONResponse:
        status_code = 200 if service.ready else 503
        return JSONResponse(
            status_code=status_code,
            content={"status": "ready" if service.ready else "not_ready"},
        )

    error_responses: dict[int | str, dict[str, Any]] = {
        422: {"model": ProblemDetails, "description": "Request validation failed"},
        500: {"model": ProblemDetails, "description": "Internal service error"},
    }

    @app.post(
        "/v1/initialize",
        response_model=InitializeResponse,
        responses=error_responses,
    )
    async def initialize(request: InitializeRequest, http_request: Request) -> InitializeResponse:
        correlation_id = request.correlation_id or _request_id(http_request)
        return service.initialize(request, correlation_id)

    @app.post(
        "/v1/compile",
        response_model=CompileResponse,
        responses=error_responses,
    )
    async def compile_sqg(request: CompileRequest, http_request: Request) -> CompileResponse:
        correlation_id = request.correlation_id or _request_id(http_request)
        return await service.compile(request, correlation_id)

    return app


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "unavailable"))


app = create_app()
