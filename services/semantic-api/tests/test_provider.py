from __future__ import annotations

import asyncio
import math

import pytest

from semantic_api.provider import (
    ProviderInvoker,
    ProviderResult,
    ProviderTimeoutError,
    StaticFixtureProvider,
)


@pytest.mark.parametrize("timeout", [0, -1, math.inf, math.nan])
def test_provider_timeout_must_be_finite_and_positive(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        ProviderInvoker(StaticFixtureProvider(), timeout_seconds=timeout)


async def test_provider_timeout_cancels_underlying_call(compile_context: object) -> None:
    provider = StaticFixtureProvider(delay_seconds=1)
    invoker = ProviderInvoker(provider, timeout_seconds=0.01)

    with pytest.raises(ProviderTimeoutError):
        await invoker.compile(compile_context)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert provider.cancelled is True


async def test_caller_cancellation_propagates(compile_context: object) -> None:
    provider = StaticFixtureProvider(delay_seconds=1)
    invoker = ProviderInvoker(provider, timeout_seconds=2)
    task = asyncio.create_task(invoker.compile(compile_context))  # type: ignore[arg-type]
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled is True


async def test_cancellation_suppressing_late_result_is_rejected(
    compile_context: object,
) -> None:
    class CancellationSuppressingProvider(StaticFixtureProvider):
        async def compile(self, context: object) -> ProviderResult:  # type: ignore[override]
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                return ProviderResult(candidate=self._quarterly_profit([]))
            raise AssertionError("provider unexpectedly completed before deadline")

    invoker = ProviderInvoker(CancellationSuppressingProvider(), timeout_seconds=0.01)

    with pytest.raises(ProviderTimeoutError):
        await invoker.compile(compile_context)  # type: ignore[arg-type]
    await asyncio.sleep(0)


async def test_static_provider_is_deterministic(compile_context: object) -> None:
    provider = StaticFixtureProvider()

    first = await provider.compile(compile_context)  # type: ignore[arg-type]
    second = await provider.compile(compile_context)  # type: ignore[arg-type]

    assert first.candidate == second.candidate
    assert first.candidate["schema_version"] == "sqg.v0"
