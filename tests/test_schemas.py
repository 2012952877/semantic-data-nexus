from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

from semantic_core.cli import _load_json
from semantic_core.models import (
    DecimalLiteral,
    IntegerLiteral,
    Ontology,
    SemanticQueryGraph,
)

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


@pytest.mark.parametrize("bare_float", [1.0, 1.1])
def test_derive_rejects_bare_float_in_schema_and_model(bare_float: float) -> None:
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
                    "right": {"kind": "literal", "value": bare_float},
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
    with pytest.raises(PydanticValidationError):
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


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_number_predicate_rejects_nonfinite_values(value: float) -> None:
    instance = {
        "contract_version": "sqg/v0",
        "query_id": "nonfinite_number",
        "ontology_version": "numbers.v1",
        "nodes": [
            {
                "id": "select_score",
                "operator": "SELECT",
                "inputs": [],
                "params": {"entity": "scores", "fields": ["score"]},
                "outputs": [{"name": "score", "data_type": "number"}],
            },
            {
                "id": "filter_score",
                "operator": "FILTER",
                "inputs": ["select_score"],
                "params": {
                    "predicate": {"field": "score", "op": "GT", "value": value}
                },
                "outputs": [{"name": "score", "data_type": "number"}],
            },
        ],
        "root": "filter_score",
    }
    with pytest.raises(PydanticValidationError, match="does not match"):
        SemanticQueryGraph.model_validate(instance)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_contract_json_loader_rejects_nonfinite_constants(
    tmp_path: Path, constant: str
) -> None:
    path = tmp_path / "nonfinite.json"
    path.write_text(f'{{"value": {constant}}}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite JSON number"):
        _load_json(path)


@pytest.mark.parametrize("missing_key", ["left_key", "right_key"])
def test_join_key_aliases_are_required_by_schema_and_model(
    missing_key: str,
) -> None:
    schema = json.loads((ROOT / "contracts" / "v0" / "sqg.schema.json").read_text())
    select = {
        "operator": "SELECT",
        "inputs": [],
        "params": {"entity": "accounts", "fields": ["account_id"]},
        "outputs": [{"name": "account_id", "data_type": "integer"}],
    }
    instance = {
        "contract_version": "sqg/v0",
        "query_id": "self_join_keys",
        "ontology_version": "self.v1",
        "nodes": [
            {"id": "select_left", **select},
            {"id": "select_right", **select},
            {
                "id": "join_accounts",
                "operator": "JOIN",
                "inputs": ["select_left", "select_right"],
                "params": {
                    "relation": "account_pair",
                    "left_key": "account_id",
                    "right_key": "account_id",
                    "kind": "INNER",
                    "fields": [
                        {
                            "source": "left",
                            "field": "account_id",
                            "name": "account_id",
                        }
                    ],
                },
                "outputs": [{"name": "account_id", "data_type": "integer"}],
            },
        ],
        "root": "join_accounts",
    }
    Draft202012Validator(schema).validate(instance)
    SemanticQueryGraph.model_validate(instance)

    del instance["nodes"][-1]["params"][missing_key]
    assert list(Draft202012Validator(schema).iter_errors(instance))
    with pytest.raises(PydanticValidationError):
        SemanticQueryGraph.model_validate(instance)


@pytest.mark.parametrize("terminator", ["\n", "\r", "\r\n"])
def test_identifier_terminal_newlines_fail_schema_and_model(
    terminator: str,
) -> None:
    ontology_schema = json.loads(
        (ROOT / "contracts" / "v0" / "ontology.schema.json").read_text()
    )
    ontology = json.loads((ROOT / "examples" / "retail-ontology.json").read_text())
    ontology["ontology_id"] += terminator
    assert list(Draft202012Validator(ontology_schema).iter_errors(ontology))
    with pytest.raises(PydanticValidationError):
        Ontology.model_validate(ontology)

    sqg_schema = json.loads(
        (ROOT / "contracts" / "v0" / "sqg.schema.json").read_text()
    )
    sqg = json.loads(
        (ROOT / "examples" / "regional-quarter-profit.json").read_text()
    )
    sqg["query_id"] += terminator
    assert list(Draft202012Validator(sqg_schema).iter_errors(sqg))
    with pytest.raises(PydanticValidationError):
        SemanticQueryGraph.model_validate(sqg)


@pytest.mark.parametrize("terminator", ["\n", "\r", "\r\n"])
@pytest.mark.parametrize(
    ("definition", "model", "kind", "value"),
    [
        ("integerLiteral", IntegerLiteral, "integer", "42"),
        ("decimalLiteral", DecimalLiteral, "decimal", "42.125"),
    ],
)
def test_tagged_number_terminal_newlines_fail_schema_and_model(
    terminator: str,
    definition: str,
    model: type,
    kind: str,
    value: str,
) -> None:
    schema = json.loads((ROOT / "contracts" / "v0" / "sqg.schema.json").read_text())
    instance = {"kind": kind, "value": value + terminator}
    assert list(
        Draft202012Validator(schema["$defs"][definition]).iter_errors(instance)
    )
    with pytest.raises(PydanticValidationError):
        model.model_validate(instance)


@pytest.mark.parametrize("terminator", ["\n", "\r", "\r\n"])
def test_sha256_requires_exactly_64_hex_characters(terminator: str) -> None:
    schema = json.loads(
        (ROOT / "contracts" / "v0" / "result-manifest.schema.json").read_text()
    )
    manifest = {
        "contract_version": "result-manifest/v0",
        "manifest_id": "manifest-1",
        "run_id": "run-1",
        "format": "parquet",
        "row_count": 1,
        "byte_count": 10,
        "parts": [{"uri": "results/part-0.parquet", "sha256": "a" * 64}],
        "committed_at": "2026-09-03T12:00:00Z",
        "committed": True,
    }
    Draft202012Validator(schema).validate(manifest)
    manifest["parts"][0]["sha256"] += terminator
    assert list(Draft202012Validator(schema).iter_errors(manifest))
