"""Run from the repository root to regenerate the additive JSON Schema contracts."""

from __future__ import annotations

import json
from pathlib import Path

from semantic_api.catalog_v1.catalog import pin_for
from semantic_api.catalog_v1.models import (
    SQGV1,
    CatalogCompileRequest,
    CatalogDocument,
    Compilation,
)
from semantic_api.catalog_v1.provider import candidate_schema


def export(directory: Path) -> None:
    for name, schema in (
        ("catalog", CatalogDocument.model_json_schema()),
        ("request", CatalogCompileRequest.model_json_schema()),
        ("compilation", Compilation.model_json_schema()),
        ("sqg", SQGV1.model_json_schema()),
        ("candidate", candidate_schema()),
    ):
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"urn:semantic-data-nexus:compiler:{name}:v1"
        (directory / f"{name}.schema.json").write_text(
            json.dumps(schema, indent=2) + "\n", encoding="utf-8"
        )
    path = directory / "examples.json"
    examples = json.loads(path.read_text(encoding="utf-8"))
    for case in examples["cases"]:
        catalog = CatalogDocument.model_validate(case["catalog"])
        case["candidate"]["graph"]["catalog"] = pin_for(catalog).model_dump(mode="json")
    path.write_text(json.dumps(examples, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    export(Path("contracts") / "compiler" / "v1")
