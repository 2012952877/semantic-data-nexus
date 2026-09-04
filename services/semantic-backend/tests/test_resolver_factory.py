from __future__ import annotations

import pytest
from query_runtime.resolver import FakeResolver

from semantic_backend.adapter import CompilerRuntimeAdapter
from semantic_backend.resolver_factory import (
    ResolverConfigurationError,
    resolver_from_environment,
)


def test_fake_resolver_accepts_bounded_observability_delay() -> None:
    adapter = CompilerRuntimeAdapter()
    resolver = resolver_from_environment(
        adapter,
        {
            "SEMANTIC_NEXUS_RESOLVER": "fake",
            "SEMANTIC_NEXUS_FAKE_DELAY_MS": "750",
        },
    )

    assert isinstance(resolver, FakeResolver)
    assert resolver._delays == {adapter.mapping.source.alias: 0.75}


@pytest.mark.parametrize("value", ["-1", "1.5", "5001", "unbounded"])
def test_fake_resolver_rejects_invalid_observability_delay(value: str) -> None:
    with pytest.raises(ResolverConfigurationError, match="integer from 0 through 5000"):
        resolver_from_environment(
            CompilerRuntimeAdapter(),
            {
                "SEMANTIC_NEXUS_RESOLVER": "fake",
                "SEMANTIC_NEXUS_FAKE_DELAY_MS": value,
            },
        )
