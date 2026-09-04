"""Offline CLI for executing only built-in validated physical-plan fixtures."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from query_runtime.coordinator import QueryCoordinator
from query_runtime.fixtures import fixture_by_name
from query_runtime.result_store import ParquetResultStore


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Execute a validated synthetic plan")
    result.add_argument(
        "fixture",
        choices=("simple", "complex", "join", "zero"),
        default="complex",
        nargs="?",
    )
    result.add_argument(
        "--result-root",
        type=Path,
        default=Path(".query-runtime-results"),
        help="Local Parquet result root",
    )
    return result


async def execute(fixture_name: str, result_root: Path) -> dict[str, object]:
    fixture = fixture_by_name(fixture_name)
    coordinator = QueryCoordinator(
        resolver=fixture.resolver,
        result_store=ParquetResultStore(result_root),
    )
    outcome = await coordinator.run(fixture.plan)
    return {
        "summary": outcome.summary.model_dump(mode="json"),
        "manifest": (
            outcome.manifest.model_dump(mode="json", by_alias=True)
            if outcome.manifest
            else None
        ),
        "lineage": outcome.lineage.model_dump(mode="json"),
    }


def main() -> None:
    args = parser().parse_args()
    print(json.dumps(asyncio.run(execute(args.fixture, args.result_root)), indent=2))


if __name__ == "__main__":
    main()
