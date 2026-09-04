from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime

from semantic_api.models import CompilationMode

from semantic_backend.adapter import CompilerRuntimeAdapter
from semantic_backend.models import (
    ExecutionMode,
    OutputMode,
    RunState,
    StartRunRequest,
)
from semantic_backend.resolver_factory import resolver_from_environment
from semantic_backend.service import OrchestrationService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic Semantic Data Nexus backend without Docker.",
    )
    parser.add_argument(
        "--scenario",
        choices=("simple", "complex"),
        default="simple",
    )
    return parser


async def run_demo(scenario: str) -> int:
    adapter = CompilerRuntimeAdapter()
    service = OrchestrationService(
        adapter=adapter,
        resolver=resolver_from_environment(adapter, {}),
    )
    mode = (
        CompilationMode.REGIONAL_QUARTERLY_PROFIT
        if scenario == "simple"
        else CompilationMode.MONTHLY_REGIONAL_COMPARISON
    )
    question = "上季度各区域利润是多少?" if scenario == "simple" else "对比各区域销售利润, 按月份"
    run_id = f"run_{uuid.uuid4().hex}"
    request = StartRunRequest(
        run_id=run_id,
        client_request_id=f"demo-{uuid.uuid4().hex[:12]}",
        workload="synthetic-profit",
        question=question,
        evaluation_clock=datetime(2024, 4, 15, 9, tzinfo=UTC),
        evaluation_timezone="Etc/UTC",
        compilation_mode=mode,
        execution_mode=ExecutionMode.THREAD,
        output_mode=OutputMode.NORMAL,
        requested_by="offline-demo",
        trace_id=f"demo-{uuid.uuid4().hex}",
    )
    try:
        await service.start(request)
        for _ in range(300):
            status = await service.get_status(run_id)
            if status.state.terminal:
                break
            await asyncio.sleep(0.05)
        else:
            return 1
        detail = await service.get_detail(run_id)
        print(
            json.dumps(
                {
                    "status": status.model_dump(mode="json", by_alias=True),
                    "detail": detail.model_dump(mode="json", by_alias=True),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if status.state is RunState.SUCCEEDED else 1
    finally:
        await service.shutdown()


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(run_demo(args.scenario)))


if __name__ == "__main__":
    main()
