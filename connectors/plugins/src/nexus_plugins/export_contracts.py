"""Generate structural v1 schemas; model validation remains the semantic acceptance gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from query_runtime.domain import V0_OPERATOR_KINDS, OperatorSpecV1, PhysicalPlan
from query_runtime.planner import ValidatedLogicalGraph

from nexus_plugins.contracts import PluginDescriptor


def schemas() -> dict[str, dict[str, Any]]:
    models: dict[str, type[BaseModel]] = {
        "plugin.schema.json": PluginDescriptor,
        "operator.schema.json": OperatorSpecV1,
        "logical-graph.schema.json": ValidatedLogicalGraph,
        "physical-plan.schema.json": PhysicalPlan,
    }
    result = {}
    for name, model in models.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://semantic-data-nexus.example.invalid/contracts/plugins/v1/{name}"
        if model in {PhysicalPlan, ValidatedLogicalGraph}:
            schema["properties"]["version"] = {"const": "query-runtime/v1", "type": "string"}
            schema["required"] = sorted(set(schema["required"]) | {"version"})
        legacy = schema.get("$defs", {}).get("OperatorSpec")
        if legacy is not None:
            legacy["properties"]["kind"] = {
                "type": "string",
                "enum": sorted(kind.value for kind in V0_OPERATOR_KINDS),
            }
        result[name] = schema
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    options = parser.parse_args()
    options.directory.mkdir(parents=True, exist_ok=True)
    for name, schema in schemas().items():
        (options.directory / name).write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
