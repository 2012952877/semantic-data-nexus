from __future__ import annotations

import json
import sys
from typing import Any


class ComposeValidationError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ComposeValidationError(message)


def validate(config: dict[str, Any]) -> None:
    services = config.get("services")
    networks = config.get("networks")
    _require(isinstance(services, dict), "Compose services are missing")
    _require(isinstance(networks, dict), "Compose networks are missing")

    semantic = services.get("semantic-backend")
    control = services.get("control-api")
    web = services.get("web")
    _require(
        all(isinstance(service, dict) for service in (semantic, control, web)),
        "Compose must define semantic-backend, control-api, and web",
    )
    assert isinstance(semantic, dict)
    assert isinstance(control, dict)
    assert isinstance(web, dict)

    _require(not semantic.get("ports"), "semantic-backend must not publish host ports")
    _require(not control.get("ports"), "control-api must not publish host ports")

    ports = web.get("ports")
    _require(isinstance(ports, list) and len(ports) == 1, "web must publish one port")
    published = ports[0]
    _require(isinstance(published, dict), "web port must use normalized Compose syntax")
    _require(published.get("host_ip") == "127.0.0.1", "web must bind only to loopback")
    _require(str(published.get("target")) == "8080", "web container port must be 8080")

    semantic_dependency = control.get("depends_on", {}).get("semantic-backend", {})
    web_dependency = web.get("depends_on", {}).get("control-api", {})
    _require(
        semantic_dependency.get("condition") == "service_healthy",
        "control-api must wait for a healthy semantic-backend",
    )
    _require(
        web_dependency.get("condition") == "service_healthy",
        "web must wait for a healthy control-api",
    )

    backend = networks.get("backend")
    _require(
        isinstance(backend, dict) and backend.get("internal") is True,
        "backend network must remain internal",
    )
    semantic_environment = semantic.get("environment", {})
    _require(
        semantic_environment.get("SEMANTIC_NEXUS_RESOLVER") == "fake",
        "base Compose must use the fake resolver",
    )
    _require(
        "DATABRICKS_TOKEN" not in semantic_environment,
        "base Compose must not inject Databricks credentials",
    )


def main() -> int:
    try:
        config = json.load(sys.stdin)
        _require(isinstance(config, dict), "Compose config must be a JSON object")
        validate(config)
    except (ComposeValidationError, json.JSONDecodeError) as error:
        print(f"COMPOSE CONFIG VALIDATION FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
