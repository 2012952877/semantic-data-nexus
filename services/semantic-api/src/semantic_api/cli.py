from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from uuid import uuid4

from semantic_api.compiler import SemanticCompiler
from semantic_api.models import CompilationMode, CompileRequest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile a synthetic question to a validated typed SQG."
    )
    parser.add_argument(
        "question",
        nargs="?",
        default="Show regional quarterly profit for 上季度",
    )
    parser.add_argument(
        "--clock",
        default="2026-08-15T09:00:00+08:00",
        help="Timezone-aware ISO 8601 evaluation clock.",
    )
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument(
        "--mode",
        choices=[item.value for item in CompilationMode],
        default=CompilationMode.REGIONAL_QUARTERLY_PROFIT.value,
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    request = CompileRequest(
        question=args.question,
        evaluation_clock=datetime.fromisoformat(args.clock),
        evaluation_timezone=args.timezone,
        compilation_mode=CompilationMode(args.mode),
    )
    compiler = SemanticCompiler.default()
    try:
        response = await compiler.compile(request, str(uuid4()))
    finally:
        await compiler.aclose()
    print(json.dumps(response.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if response.normalized_sqg is not None else 2


def main() -> None:
    raise SystemExit(asyncio.run(run(build_parser().parse_args())))


if __name__ == "__main__":
    main()
