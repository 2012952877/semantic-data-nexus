from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest_asyncio
from semantic_api.models import CompilationMode

from semantic_backend.models import ExecutionOptions, StartRunRequest
from semantic_backend.service import OrchestrationService


@pytest_asyncio.fixture
async def service():
    orchestrator = OrchestrationService()
    yield orchestrator
    await orchestrator.shutdown()


def request_for(
    run_id: str,
    *,
    mode: CompilationMode = CompilationMode.REGIONAL_QUARTERLY_PROFIT,
) -> StartRunRequest:
    question = (
        "上季度各区域利润是多少?"
        if mode is CompilationMode.REGIONAL_QUARTERLY_PROFIT
        else "对比各区域销售利润, 按月份"
    )
    return StartRunRequest(
        run_id=run_id,
        question=question,
        requested_by="synthetic-demo-user",
        trace_id=f"trace-{run_id[-8:]}",
        evaluation_clock=datetime(2024, 4, 15, 9, tzinfo=UTC),
        evaluation_timezone="UTC",
        compilation_mode=mode,
        execution_options=ExecutionOptions(max_rows=1_000, timeout_seconds=10),
    )


async def wait_for_terminal(
    service: OrchestrationService,
    run_id: str,
    *,
    attempts: int = 200,
):
    for _ in range(attempts):
        status = await service.get_status(run_id)
        if status.state.terminal:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError("run did not reach a terminal state")
