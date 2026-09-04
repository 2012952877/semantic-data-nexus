from __future__ import annotations

import unittest
from decimal import Decimal

from scripts.full_stack_smoke import SmokeFailure, _decimal_value, _rows_by_key
from scripts.validate_compose_config import ComposeValidationError, validate


def compose_config(host_ip: str = "127.0.0.1") -> dict[str, object]:
    return {
        "services": {
            "semantic-backend": {
                "environment": {
                    "SEMANTIC_NEXUS_RESOLVER": "fake",
                },
            },
            "control-api": {
                "depends_on": {
                    "semantic-backend": {"condition": "service_healthy"},
                },
            },
            "web": {
                "depends_on": {
                    "control-api": {"condition": "service_healthy"},
                },
                "ports": [
                    {
                        "host_ip": host_ip,
                        "published": "8080",
                        "target": 8080,
                    },
                ],
            },
        },
        "networks": {
            "backend": {"internal": True},
            "edge": {},
        },
    }


def live_compose_config() -> dict[str, object]:
    config = compose_config()
    services = config["services"]
    assert isinstance(services, dict)
    semantic = services["semantic-backend"]
    assert isinstance(semantic, dict)
    semantic["environment"] = {
        "SEMANTIC_NEXUS_RESOLVER": "databricks",
        "DATABRICKS_WORKSPACE_HOST": "https://example.invalid",
        "DATABRICKS_WAREHOUSE_ID": "ci-placeholder",
        "DATABRICKS_TOKEN": "runtime-placeholder",
    }
    semantic["networks"] = {"backend": None, "databricks-egress": None}
    networks = config["networks"]
    assert isinstance(networks, dict)
    networks["databricks-egress"] = {}
    return config


def result(data_type: str, value: object) -> dict[str, object]:
    return {
        "columns": [
            {
                "key": "profit",
                "label": "Profit",
                "dataType": data_type,
                "format": "currency",
                "nullable": False,
            },
        ],
        "rows": [[value]],
    }


class ComposeConfigTests(unittest.TestCase):
    def test_accepts_loopback_only_fake_stack(self) -> None:
        validate(compose_config())

    def test_rejects_development_identity_on_all_interfaces(self) -> None:
        with self.assertRaisesRegex(ComposeValidationError, "bind only to loopback"):
            validate(compose_config("0.0.0.0"))

    def test_live_profile_requires_egress_and_explicit_configuration(self) -> None:
        validate(live_compose_config(), live=True)
        config = live_compose_config()
        services = config["services"]
        assert isinstance(services, dict)
        semantic = services["semantic-backend"]
        assert isinstance(semantic, dict)
        semantic["networks"] = {"backend": None}
        with self.assertRaisesRegex(ComposeValidationError, "egress network"):
            validate(config, live=True)


class ResultSchemaTests(unittest.TestCase):
    def test_decimal_requires_canonical_fixed_point_text(self) -> None:
        rows, columns = _rows_by_key(result("decimal", "2334.00"))
        self.assertEqual(rows, [{"profit": "2334.00"}])
        self.assertEqual(columns["profit"]["dataType"], "decimal")
        self.assertEqual(_decimal_value(rows[0]["profit"]), Decimal("2334.00"))

    def test_decimal_rejects_json_number_and_noncanonical_text(self) -> None:
        for value in (2334.0, "02334.00", "-0.00", "1e2"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    SmokeFailure,
                    "declared column type",
                ),
            ):
                _rows_by_key(result("decimal", value))

    def test_float_requires_finite_bounded_json_number(self) -> None:
        rows, _ = _rows_by_key(result("float", 2334.0))
        self.assertEqual(rows, [{"profit": 2334.0}])
        for value in ("2334.0", float("inf"), 9_007_199_254_740_992):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    SmokeFailure,
                    "declared column type",
                ),
            ):
                _rows_by_key(result("float", value))


if __name__ == "__main__":
    unittest.main()
