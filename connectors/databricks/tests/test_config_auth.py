from __future__ import annotations

import pytest

from semantic_data_nexus_databricks.auth import EnvironmentPatProvider
from semantic_data_nexus_databricks.config import ResolverConfig
from semantic_data_nexus_databricks.exceptions import AuthenticationError, ConfigurationError
from semantic_data_nexus_databricks.models import FetchDisposition, ResultFormat


def test_host_and_http_path_are_normalized() -> None:
    value = ResolverConfig(
        workspace_host=" WORKSPACE.EXAMPLE.INVALID/ ",
        http_path="sql/1.0/warehouses/warehouse-test",
    )
    assert value.workspace_host == "https://workspace.example.invalid"
    assert value.warehouse_id == "warehouse-test"
    assert value.http_path == "/sql/1.0/warehouses/warehouse-test"


@pytest.mark.parametrize(
    ("host", "warehouse_id"),
    [
        ("http://workspace.example.invalid", "warehouse-test"),
        ("https://workspace.example.invalid/path", "warehouse-test"),
        ("https://user@workspace.example.invalid", "warehouse-test"),
        ("https://workspace.example.invalid?query=value", "warehouse-test"),
        ("https://[2001:db8::1]", "warehouse-test"),
        ("https://workspace.example.invalid", "invalid/id"),
    ],
)
def test_invalid_configuration_is_rejected(host: str, warehouse_id: str) -> None:
    with pytest.raises(ConfigurationError):
        ResolverConfig(workspace_host=host, warehouse_id=warehouse_id)


def test_inline_arrow_combination_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        ResolverConfig(
            workspace_host="workspace.example.invalid",
            warehouse_id="warehouse-test",
            disposition=FetchDisposition.INLINE,
            result_format=ResultFormat.ARROW_STREAM,
        )


async def test_environment_pat_header_and_repr_do_not_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNTHETIC_PAT", "synthetic-secret-value")
    token = await EnvironmentPatProvider("SYNTHETIC_PAT").get_token()
    assert token.authorization_header() == {
        "Authorization": "Bearer synthetic-secret-value"
    }
    assert "synthetic-secret-value" not in repr(token)


async def test_missing_environment_pat_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SYNTHETIC_PAT", raising=False)
    with pytest.raises(AuthenticationError, match="SYNTHETIC_PAT"):
        await EnvironmentPatProvider("SYNTHETIC_PAT").get_token()
