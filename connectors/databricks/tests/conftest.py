from __future__ import annotations

from semantic_data_nexus_databricks.auth import BearerToken, BearerTokenProvider
from semantic_data_nexus_databricks.config import ResolverConfig
from semantic_data_nexus_databricks.testing import FakeTransport


class SyntheticTokenProvider(BearerTokenProvider):
    async def get_token(self) -> BearerToken:
        return BearerToken("synthetic-test-token")


def config(**overrides: object) -> ResolverConfig:
    values: dict[str, object] = {
        "workspace_host": "workspace.example.invalid",
        "warehouse_id": "warehouse-test",
        "poll_initial_seconds": 0.001,
        "poll_max_seconds": 0.002,
    }
    values.update(overrides)
    return ResolverConfig(**values)  # type: ignore[arg-type]


def fake_transport() -> FakeTransport:
    return FakeTransport()
