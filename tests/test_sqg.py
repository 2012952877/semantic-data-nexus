from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from semantic_core.models import Ontology, SemanticQueryGraph
from semantic_core.validation import SemanticValidationError, validate_sqg

ROOT = Path(__file__).parents[1]


@pytest.fixture
def ontology_data() -> dict:
    return json.loads((ROOT / "examples" / "retail-ontology.json").read_text())


@pytest.fixture
def sqg_data() -> dict:
    return json.loads((ROOT / "examples" / "regional-quarter-profit.json").read_text())


def test_valid_dag(sqg_data: dict, ontology_data: dict) -> None:
    sqg = SemanticQueryGraph.model_validate(sqg_data)
    ontology = Ontology.model_validate(ontology_data)
    validate_sqg(sqg, ontology)
    assert sqg.root == "project_answer"


@pytest.mark.parametrize(
    ("dependency", "message"),
    [
        ("filter_quarter", "cannot depend on itself"),
        ("aggregate_profit", "forward dependency"),
        ("not_present", "missing input"),
    ],
)
def test_rejects_invalid_dependencies(
    sqg_data: dict, dependency: str, message: str
) -> None:
    candidate = copy.deepcopy(sqg_data)
    candidate["nodes"][1]["inputs"] = [dependency]
    with pytest.raises(ValidationError, match=message):
        SemanticQueryGraph.model_validate(candidate)


def test_rejects_duplicate_node_ids(sqg_data: dict) -> None:
    candidate = copy.deepcopy(sqg_data)
    candidate["nodes"][1]["id"] = candidate["nodes"][0]["id"]
    with pytest.raises(ValidationError, match="duplicate node IDs"):
        SemanticQueryGraph.model_validate(candidate)


def test_root_must_cover_every_node(sqg_data: dict) -> None:
    candidate = copy.deepcopy(sqg_data)
    candidate["nodes"].append(
        {
            "id": "disconnected_sales",
            "operator": "SELECT",
            "inputs": [],
            "params": {"entity": "sales", "fields": ["region"]},
            "outputs": [
                {"name": "region", "data_type": "string", "nullable": False}
            ],
        }
    )
    with pytest.raises(ValidationError, match="outside root ancestor closure"):
        SemanticQueryGraph.model_validate(candidate)


def test_root_cannot_have_downstream_nodes(sqg_data: dict) -> None:
    candidate = copy.deepcopy(sqg_data)
    candidate["root"] = "aggregate_profit"
    with pytest.raises(ValidationError, match="outside root ancestor closure"):
        SemanticQueryGraph.model_validate(candidate)


def test_rejects_invalid_outputs(sqg_data: dict) -> None:
    candidate = copy.deepcopy(sqg_data)
    candidate["nodes"][2]["outputs"][-1]["name"] = "unexpected"
    with pytest.raises(ValidationError, match="expected"):
        SemanticQueryGraph.model_validate(candidate)


def test_rejects_passthrough_output_type_changes(sqg_data: dict) -> None:
    candidate = copy.deepcopy(sqg_data)
    candidate["nodes"][1]["outputs"][0]["data_type"] = "integer"
    with pytest.raises(ValidationError, match="expected 'string'"):
        SemanticQueryGraph.model_validate(candidate)


def test_filter_value_must_match_field_type(sqg_data: dict) -> None:
    candidate = copy.deepcopy(sqg_data)
    candidate["nodes"][1]["params"]["predicate"] = {
        "field": "profit",
        "op": "GT",
        "value": "not-a-number",
    }
    with pytest.raises(ValidationError, match="does not match"):
        SemanticQueryGraph.model_validate(candidate)


def test_metric_source_field_must_reach_aggregate(
    sqg_data: dict, ontology_data: dict
) -> None:
    candidate = copy.deepcopy(sqg_data)
    project = {
        "id": "project_region",
        "operator": "PROJECT",
        "inputs": ["filter_quarter"],
        "params": {"fields": ["region", "quarter"]},
        "outputs": [
            {"name": "region", "data_type": "string", "nullable": False},
            {"name": "quarter", "data_type": "string", "nullable": False},
        ],
    }
    candidate["nodes"].insert(2, project)
    candidate["nodes"][3]["inputs"] = ["project_region"]
    sqg = SemanticQueryGraph.model_validate(candidate)
    with pytest.raises(SemanticValidationError, match=r"source field sales\.profit"):
        validate_sqg(sqg, Ontology.model_validate(ontology_data))


def test_rejects_unknown_operator(sqg_data: dict) -> None:
    candidate = copy.deepcopy(sqg_data)
    candidate["nodes"][1]["operator"] = "EXECUTE_SQL"
    with pytest.raises(ValidationError, match="Input tag"):
        SemanticQueryGraph.model_validate(candidate)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("nodes", 1, "params", "predicate", "op"), "LIKE"),
        (("nodes", 3, "params", "keys", 0, "direction"), "SIDEWAYS"),
        (("nodes", 2, "params", "measures"), "profit"),
    ],
)
def test_operator_parameters_are_typed(
    sqg_data: dict, path: tuple, value: object
) -> None:
    candidate = copy.deepcopy(sqg_data)
    target = candidate
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        SemanticQueryGraph.model_validate(candidate)


def test_ontology_membership_is_exact(sqg_data: dict, ontology_data: dict) -> None:
    candidate = copy.deepcopy(sqg_data)
    candidate["nodes"] = candidate["nodes"][:1]
    candidate["root"] = candidate["nodes"][0]["id"]
    candidate["nodes"][0]["params"]["fields"][0] = "Region"
    candidate["nodes"][0]["outputs"][0]["name"] = "Region"
    sqg = SemanticQueryGraph.model_validate(candidate)
    ontology = Ontology.model_validate(ontology_data)
    with pytest.raises(SemanticValidationError, match="sales.Region"):
        validate_sqg(sqg, ontology)


def test_metric_membership_is_exact(sqg_data: dict, ontology_data: dict) -> None:
    candidate = copy.deepcopy(sqg_data)
    candidate["nodes"][2]["params"]["measures"][0]["metric"] = "Profit"
    sqg = SemanticQueryGraph.model_validate(candidate)
    ontology = Ontology.model_validate(ontology_data)
    with pytest.raises(SemanticValidationError, match="unknown metric 'Profit'"):
        validate_sqg(sqg, ontology)
