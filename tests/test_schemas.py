from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]


def test_all_contract_schemas_are_valid() -> None:
    schemas = sorted((ROOT / "contracts" / "v0").glob("*.schema.json"))
    assert schemas
    for path in schemas:
        Draft202012Validator.check_schema(json.loads(path.read_text()))


def test_examples_match_json_schemas() -> None:
    pairs = [
        ("ontology.schema.json", "retail-ontology.json"),
        ("sqg.schema.json", "regional-quarter-profit.json"),
    ]
    for schema_name, example_name in pairs:
        schema = json.loads((ROOT / "contracts" / "v0" / schema_name).read_text())
        instance = json.loads((ROOT / "examples" / example_name).read_text())
        Draft202012Validator(schema).validate(instance)

