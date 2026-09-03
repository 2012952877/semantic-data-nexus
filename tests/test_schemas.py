from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

from semantic_core.models import Ontology, SemanticQueryGraph

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


def test_boolean_strictness_matches_json_schema() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "v0" / "ontology.schema.json").read_text()
    )
    instance = json.loads((ROOT / "examples" / "retail-ontology.json").read_text())
    instance["entities"][0]["fields"][0]["nullable"] = 1
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(schema).validate(instance)
    with pytest.raises(PydanticValidationError):
        Ontology.model_validate(instance)


@pytest.mark.parametrize(
    ("op", "value", "valid"),
    [
        ("EQ", ["east"], False),
        ("GT", [1], False),
        ("IN", "east", False),
        ("NOT_IN", [], False),
        ("IN", ["east"], True),
        ("EQ", "east", True),
    ],
)
def test_predicate_shape_matches_schema_and_model(
    op: str, value: object, valid: bool
) -> None:
    schema = json.loads((ROOT / "contracts" / "v0" / "sqg.schema.json").read_text())
    instance = json.loads(
        (ROOT / "examples" / "regional-quarter-profit.json").read_text()
    )
    instance["nodes"][1]["params"]["predicate"] = {
        "field": "region",
        "op": op,
        "value": value,
    }
    schema_errors = list(Draft202012Validator(schema).iter_errors(instance))
    if valid:
        assert schema_errors == []
        SemanticQueryGraph.model_validate(instance)
    else:
        assert schema_errors
        with pytest.raises(PydanticValidationError):
            SemanticQueryGraph.model_validate(instance)


def test_decimal_literal_matches_schema_and_model() -> None:
    schema = json.loads((ROOT / "contracts" / "v0" / "sqg.schema.json").read_text())
    instance = json.loads(
        (ROOT / "examples" / "regional-quarter-profit.json").read_text()
    )
    exact = "12345678901234567890.12345678901234567890"
    instance["nodes"][1]["params"]["predicate"] = {
        "field": "profit",
        "op": "GT",
        "value": {"kind": "decimal", "value": exact},
    }
    Draft202012Validator(schema).validate(instance)
    sqg = SemanticQueryGraph.model_validate(instance)
    assert sqg.nodes[1].params.predicate.value.value == exact


def test_derive_rejects_bare_float_in_schema_and_model() -> None:
    schema = json.loads((ROOT / "contracts" / "v0" / "sqg.schema.json").read_text())
    instance = json.loads(
        (ROOT / "examples" / "regional-quarter-profit.json").read_text()
    )
    derive = {
        "id": "derive_float",
        "operator": "DERIVE",
        "inputs": ["filter_quarter"],
        "params": {
            "expressions": [
                {
                    "name": "scaled_profit",
                    "op": "MULTIPLY",
                    "left": {"kind": "field", "field": "profit"},
                    "right": {"kind": "literal", "value": 1.1},
                }
            ]
        },
        "outputs": [
            {"name": "region", "data_type": "string", "nullable": False},
            {"name": "quarter", "data_type": "string", "nullable": False},
            {"name": "profit", "data_type": "decimal", "nullable": False},
            {"name": "scaled_profit", "data_type": "decimal", "nullable": False},
        ],
    }
    instance["nodes"] = instance["nodes"][:2] + [derive]
    instance["root"] = "derive_float"
    assert list(Draft202012Validator(schema).iter_errors(instance))
    with pytest.raises(PydanticValidationError, match="tagged decimal"):
        SemanticQueryGraph.model_validate(instance)


def test_run_events_require_scope_identifiers() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "v0" / "run-event.schema.json").read_text()
    )
    validator = Draft202012Validator(schema)
    base = {
        "contract_version": "run-event/v0",
        "run_id": "run-1",
        "sequence": 1,
        "emitted_at": "2026-09-03T12:00:00Z",
        "payload": {},
    }

    valid_stage = {**base, "event_type": "stage.started", "stage_id": "compile"}
    valid_node = {
        **base,
        "event_type": "node.started",
        "stage_id": "compile",
        "node_id": "filter",
    }
    validator.validate(valid_stage)
    validator.validate(valid_node)

    invalid_stage = {**base, "event_type": "stage.started"}
    invalid_node = {**base, "event_type": "node.started", "stage_id": "compile"}
    assert list(validator.iter_errors(invalid_stage))
    assert list(validator.iter_errors(invalid_node))
