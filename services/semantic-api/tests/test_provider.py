from __future__ import annotations

import asyncio

import pytest

from semantic_api.provider import ProviderInvoker, ProviderTimeoutError, StaticFixtureProvider


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


async def test_static_provider_is_deterministic(compile_context: object) -> None:
    provider = StaticFixtureProvider()

    first = await provider.compile(compile_context)  # type: ignore[arg-type]
    second = await provider.compile(compile_context)  # type: ignore[arg-type]

    assert first.candidate == second.candidate
    assert first.candidate["schema_version"] == "sqg.v0"
