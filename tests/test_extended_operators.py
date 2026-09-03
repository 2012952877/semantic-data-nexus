from __future__ import annotations

import copy

import pytest

from semantic_core.models import DecimalLiteral, Ontology, SemanticQueryGraph
from semantic_core.validation import SemanticValidationError, validate_sqg


def _ontology() -> Ontology:
    return Ontology.model_validate(
        {
            "contract_version": "ontology/v0",
            "ontology_id": "operator_demo",
            "version": "operator.v1",
            "entities": [
                {
                    "id": "sales",
                    "fields": [
                        {"id": "region", "data_type": "string"},
                        {"id": "quarter", "data_type": "string"},
                        {"id": "profit", "data_type": "decimal"},
                    ],
                },
                {
                    "id": "regions",
                    "fields": [
                        {"id": "region", "data_type": "string"},
                        {"id": "manager", "data_type": "string"},
                    ],
                },
            ],
            "metrics": [],
            "relations": [
                {
                    "id": "sales_region",
                    "left_entity": "sales",
                    "right_entity": "regions",
                    "left_field": "region",
                    "right_field": "region",
                    "cardinality": "many_to_one",
                }
            ],
            "resolvers": [],
        }
    )


def _sales_select() -> dict:
    return {
        "id": "select_sales",
        "operator": "SELECT",
        "inputs": [],
        "params": {
            "entity": "sales",
            "fields": ["region", "quarter", "profit"],
        },
        "outputs": [
            {"name": "region", "data_type": "string"},
            {"name": "quarter", "data_type": "string"},
            {"name": "profit", "data_type": "decimal"},
        ],
    }


def test_valid_derive_expression() -> None:
    data = {
        "contract_version": "sqg/v0",
        "query_id": "derive_demo",
        "ontology_version": "operator.v1",
        "nodes": [
            _sales_select(),
            {
                "id": "derive_double_profit",
                "operator": "DERIVE",
                "inputs": ["select_sales"],
                "params": {
                    "expressions": [
                        {
                            "name": "double_profit",
                            "op": "MULTIPLY",
                            "left": {"kind": "field", "field": "profit"},
                            "right": {
                                "kind": "literal",
                                "value": {"kind": "integer", "value": "2"},
                            },
                        }
                    ]
                },
                "outputs": [
                    {"name": "region", "data_type": "string"},
                    {"name": "quarter", "data_type": "string"},
                    {"name": "profit", "data_type": "decimal"},
                    {"name": "double_profit", "data_type": "decimal"},
                ],
            },
        ],
        "root": "derive_double_profit",
    }
    sqg = SemanticQueryGraph.model_validate(data)
    validate_sqg(sqg, _ontology())
    assert sqg.nodes[-1].operator == "DERIVE"


def test_decimal_literal_preserves_exact_text() -> None:
    exact = "12345678901234567890.12345678901234567890"
    data = {
        "contract_version": "sqg/v0",
        "query_id": "decimal_demo",
        "ontology_version": "operator.v1",
        "nodes": [
            _sales_select(),
            {
                "id": "derive_precise_profit",
                "operator": "DERIVE",
                "inputs": ["select_sales"],
                "params": {
                    "expressions": [
                        {
                            "name": "precise_profit",
                            "op": "MULTIPLY",
                            "left": {"kind": "field", "field": "profit"},
                            "right": {
                                "kind": "literal",
                                "value": {"kind": "decimal", "value": exact},
                            },
                        }
                    ]
                },
                "outputs": [
                    {"name": "region", "data_type": "string"},
                    {"name": "quarter", "data_type": "string"},
                    {"name": "profit", "data_type": "decimal"},
                    {"name": "precise_profit", "data_type": "decimal"},
                ],
            },
        ],
        "root": "derive_precise_profit",
    }
    sqg = SemanticQueryGraph.model_validate(data)
    literal = sqg.nodes[-1].params.expressions[0].right
    assert isinstance(literal.value, DecimalLiteral)
    assert literal.value.value == exact
    dumped = sqg.model_dump(mode="json")
    assert dumped["nodes"][-1]["params"]["expressions"][0]["right"]["value"]["value"] == exact


def test_derived_lineage_is_not_a_direct_metric_binding() -> None:
    ontology_data = _ontology().model_dump(mode="json")
    ontology_data["metrics"] = [
        {
            "id": "profit_total",
            "entity": "sales",
            "field": "profit",
            "aggregation": "SUM",
        }
    ]
    ontology = Ontology.model_validate(ontology_data)
    data = {
        "contract_version": "sqg/v0",
        "query_id": "derived_metric_binding",
        "ontology_version": "operator.v1",
        "nodes": [
            _sales_select(),
            {
                "id": "derive_profit",
                "operator": "DERIVE",
                "inputs": ["select_sales"],
                "params": {
                    "expressions": [
                        {
                            "name": "derived_profit",
                            "op": "ADD",
                            "left": {"kind": "field", "field": "profit"},
                            "right": {
                                "kind": "literal",
                                "value": {"kind": "integer", "value": "0"},
                            },
                        }
                    ]
                },
                "outputs": [
                    {"name": "region", "data_type": "string"},
                    {"name": "quarter", "data_type": "string"},
                    {"name": "profit", "data_type": "decimal"},
                    {"name": "derived_profit", "data_type": "decimal"},
                ],
            },
            {
                "id": "project_derived",
                "operator": "PROJECT",
                "inputs": ["derive_profit"],
                "params": {"fields": ["derived_profit"]},
                "outputs": [{"name": "derived_profit", "data_type": "decimal"}],
            },
            {
                "id": "aggregate_profit",
                "operator": "AGGREGATE",
                "inputs": ["project_derived"],
                "params": {
                    "measures": [
                        {
                            "metric": "profit_total",
                            "source": "derived_profit",
                            "name": "profit_total"
                        }
                    ]
                },
                "outputs": [{"name": "profit_total", "data_type": "decimal"}],
            },
        ],
        "root": "aggregate_profit",
    }
    sqg = SemanticQueryGraph.model_validate(data)
    with pytest.raises(SemanticValidationError, match="source field sales.profit"):
        validate_sqg(sqg, ontology)


def test_derived_lineage_is_not_a_direct_join_key() -> None:
    ontology = Ontology.model_validate(
        {
            "contract_version": "ontology/v0",
            "ontology_id": "join_binding_demo",
            "version": "join.v1",
            "entities": [
                {
                    "id": "orders",
                    "fields": [{"id": "customer_id", "data_type": "integer"}],
                },
                {
                    "id": "customers",
                    "fields": [{"id": "customer_id", "data_type": "integer"}],
                },
            ],
            "relations": [
                {
                    "id": "order_customer",
                    "left_entity": "orders",
                    "right_entity": "customers",
                    "left_field": "customer_id",
                    "right_field": "customer_id",
                    "cardinality": "many_to_one",
                }
            ],
        }
    )
    data = {
        "contract_version": "sqg/v0",
        "query_id": "derived_join_binding",
        "ontology_version": "join.v1",
        "nodes": [
            {
                "id": "select_orders",
                "operator": "SELECT",
                "inputs": [],
                "params": {"entity": "orders", "fields": ["customer_id"]},
                "outputs": [{"name": "customer_id", "data_type": "integer"}],
            },
            {
                "id": "derive_customer_id",
                "operator": "DERIVE",
                "inputs": ["select_orders"],
                "params": {
                    "expressions": [
                        {
                            "name": "derived_customer_id",
                            "op": "ADD",
                            "left": {"kind": "field", "field": "customer_id"},
                            "right": {
                                "kind": "literal",
                                "value": {"kind": "integer", "value": "0"},
                            },
                        }
                    ]
                },
                "outputs": [
                    {"name": "customer_id", "data_type": "integer"},
                    {"name": "derived_customer_id", "data_type": "integer"},
                ],
            },
            {
                "id": "project_derived_id",
                "operator": "PROJECT",
                "inputs": ["derive_customer_id"],
                "params": {"fields": ["derived_customer_id"]},
                "outputs": [
                    {"name": "derived_customer_id", "data_type": "integer"}
                ],
            },
            {
                "id": "select_customers",
                "operator": "SELECT",
                "inputs": [],
                "params": {"entity": "customers", "fields": ["customer_id"]},
                "outputs": [{"name": "customer_id", "data_type": "integer"}],
            },
            {
                "id": "join_customer",
                "operator": "JOIN",
                "inputs": ["project_derived_id", "select_customers"],
                "params": {
                    "relation": "order_customer",
                    "kind": "INNER",
                    "fields": [
                        {
                            "source": "left",
                            "field": "derived_customer_id",
                            "name": "order_customer_id",
                        },
                        {
                            "source": "right",
                            "field": "customer_id",
                            "name": "customer_id",
                        },
                    ],
                },
                "outputs": [
                    {"name": "order_customer_id", "data_type": "integer"},
                    {"name": "customer_id", "data_type": "integer"},
                ],
            },
        ],
        "root": "join_customer",
    }
    sqg = SemanticQueryGraph.model_validate(data)
    with pytest.raises(SemanticValidationError, match="requires one direct join-key"):
        validate_sqg(sqg, ontology)


def test_derive_output_type_is_inferred() -> None:
    data = {
        "contract_version": "sqg/v0",
        "query_id": "invalid_derive_demo",
        "ontology_version": "operator.v1",
        "nodes": [
            _sales_select(),
            {
                "id": "derive_double_profit",
                "operator": "DERIVE",
                "inputs": ["select_sales"],
                "params": {
                    "expressions": [
                        {
                            "name": "double_profit",
                            "op": "MULTIPLY",
                            "left": {"kind": "field", "field": "profit"},
                            "right": {
                                "kind": "literal",
                                "value": {"kind": "integer", "value": "2"},
                            },
                        }
                    ]
                },
                "outputs": [
                    {"name": "region", "data_type": "string"},
                    {"name": "quarter", "data_type": "string"},
                    {"name": "profit", "data_type": "decimal"},
                    {"name": "double_profit", "data_type": "string"},
                ],
            },
        ],
        "root": "derive_double_profit",
    }
    with pytest.raises(ValueError, match="expected 'decimal'"):
        SemanticQueryGraph.model_validate(data)


def test_filter_ordering_rejects_null() -> None:
    data = {
        "contract_version": "sqg/v0",
        "query_id": "null_filter_demo",
        "ontology_version": "operator.v1",
        "nodes": [
            _sales_select(),
            {
                "id": "filter_profit",
                "operator": "FILTER",
                "inputs": ["select_sales"],
                "params": {
                    "predicate": {"field": "profit", "op": "GT", "value": None}
                },
                "outputs": _sales_select()["outputs"],
            },
        ],
        "root": "filter_profit",
    }
    with pytest.raises(ValueError, match="does not accept null"):
        SemanticQueryGraph.model_validate(data)


def test_ontology_rejects_sum_of_string() -> None:
    data = _ontology().model_dump(mode="json")
    data["metrics"] = [
        {
            "id": "invalid_region_sum",
            "entity": "sales",
            "field": "region",
            "aggregation": "SUM",
        }
    ]
    with pytest.raises(ValueError, match="SUM does not support"):
        Ontology.model_validate(data)


def test_ontology_rejects_relation_key_type_mismatch() -> None:
    data = _ontology().model_dump(mode="json")
    regions = next(entity for entity in data["entities"] if entity["id"] == "regions")
    region = next(field for field in regions["fields"] if field["id"] == "region")
    region["data_type"] = "integer"
    with pytest.raises(ValueError, match="incompatible key types"):
        Ontology.model_validate(data)


def test_date_filter_requires_iso_literal() -> None:
    data = {
        "contract_version": "sqg/v0",
        "query_id": "date_filter_demo",
        "ontology_version": "operator.v1",
        "nodes": [
            {
                "id": "select_dates",
                "operator": "SELECT",
                "inputs": [],
                "params": {"entity": "sales", "fields": ["sold_on"]},
                "outputs": [{"name": "sold_on", "data_type": "date"}],
            },
            {
                "id": "filter_dates",
                "operator": "FILTER",
                "inputs": ["select_dates"],
                "params": {
                    "predicate": {
                        "field": "sold_on",
                        "op": "GTE",
                        "value": "not-a-date",
                    }
                },
                "outputs": [{"name": "sold_on", "data_type": "date"}],
            },
        ],
        "root": "filter_dates",
    }
    with pytest.raises(ValueError, match="does not match"):
        SemanticQueryGraph.model_validate(data)


def test_valid_pivot_and_join() -> None:
    data = {
        "contract_version": "sqg/v0",
        "query_id": "pivot_join_demo",
        "ontology_version": "operator.v1",
        "nodes": [
            _sales_select(),
            {
                "id": "pivot_quarter",
                "operator": "PIVOT",
                "inputs": ["select_sales"],
                "params": {
                    "index": ["region"],
                    "column": "quarter",
                    "value": "profit",
                    "aggregation": "SUM",
                    "expected_columns": ["q1", "q2"],
                },
                "outputs": [
                    {"name": "region", "data_type": "string"},
                    {"name": "q1", "data_type": "decimal"},
                    {"name": "q2", "data_type": "decimal"},
                ],
            },
            {
                "id": "select_regions",
                "operator": "SELECT",
                "inputs": [],
                "params": {
                    "entity": "regions",
                    "fields": ["region", "manager"],
                },
                "outputs": [
                    {"name": "region", "data_type": "string"},
                    {"name": "manager", "data_type": "string"},
                ],
            },
            {
                "id": "join_manager",
                "operator": "JOIN",
                "inputs": ["pivot_quarter", "select_regions"],
                "params": {
                    "relation": "sales_region",
                    "kind": "LEFT",
                    "fields": [
                        {"source": "left", "field": "region", "name": "region"},
                        {"source": "left", "field": "q1", "name": "q1"},
                        {"source": "right", "field": "manager", "name": "manager"},
                    ],
                },
                "outputs": [
                    {"name": "region", "data_type": "string"},
                    {"name": "q1", "data_type": "decimal"},
                    {"name": "manager", "data_type": "string"},
                ],
            },
        ],
        "root": "join_manager",
    }
    sqg = SemanticQueryGraph.model_validate(data)
    validate_sqg(sqg, _ontology())
    assert {node.operator for node in sqg.nodes} == {"SELECT", "PIVOT", "JOIN"}

    invalid = copy.deepcopy(data)
    invalid["nodes"][-1]["params"]["relation"] = "Sales_Region"
    with pytest.raises(SemanticValidationError, match="unknown relation"):
        validate_sqg(SemanticQueryGraph.model_validate(invalid), _ontology())

    missing_keys = copy.deepcopy(data)
    missing_keys["nodes"][1] = {
        "id": "project_q1",
        "operator": "PROJECT",
        "inputs": ["select_sales"],
        "params": {"fields": ["profit"]},
        "outputs": [{"name": "profit", "data_type": "decimal"}],
    }
    missing_keys["nodes"][-1]["inputs"][0] = "project_q1"
    missing_keys["nodes"][-1]["params"]["fields"][0] = {
        "source": "left",
        "field": "profit",
        "name": "profit",
    }
    missing_keys["nodes"][-1]["params"]["fields"].pop(1)
    missing_keys["nodes"][-1]["outputs"] = [
        {"name": "profit", "data_type": "decimal"},
        {"name": "manager", "data_type": "string"},
    ]
    sqg_without_key = SemanticQueryGraph.model_validate(missing_keys)
    with pytest.raises(SemanticValidationError, match="requires one direct join-key"):
        validate_sqg(sqg_without_key, _ontology())


def test_reversed_join_orientation_uses_matching_direct_bindings() -> None:
    ontology = Ontology.model_validate(
        {
            "contract_version": "ontology/v0",
            "ontology_id": "orientation_demo",
            "version": "orientation.v1",
            "entities": [
                {
                    "id": "accounts",
                    "fields": [{"id": "account_id", "data_type": "integer"}],
                },
                {
                    "id": "orders",
                    "fields": [{"id": "account_id", "data_type": "integer"}],
                },
            ],
            "relations": [
                {
                    "id": "account_orders",
                    "left_entity": "accounts",
                    "right_entity": "orders",
                    "left_field": "account_id",
                    "right_field": "account_id",
                    "cardinality": "one_to_many",
                }
            ],
        }
    )
    data = {
        "contract_version": "sqg/v0",
        "query_id": "reversed_orientation",
        "ontology_version": "orientation.v1",
        "nodes": [
            {
                "id": "select_accounts",
                "operator": "SELECT",
                "inputs": [],
                "params": {"entity": "accounts", "fields": ["account_id"]},
                "outputs": [{"name": "account_id", "data_type": "integer"}],
            },
            {
                "id": "select_orders",
                "operator": "SELECT",
                "inputs": [],
                "params": {"entity": "orders", "fields": ["account_id"]},
                "outputs": [{"name": "account_id", "data_type": "integer"}],
            },
            {
                "id": "join_once",
                "operator": "JOIN",
                "inputs": ["select_accounts", "select_orders"],
                "params": {
                    "relation": "account_orders",
                    "kind": "INNER",
                    "fields": [
                        {
                            "source": "right",
                            "field": "account_id",
                            "name": "order_account_id",
                        }
                    ],
                },
                "outputs": [
                    {"name": "order_account_id", "data_type": "integer"}
                ],
            },
            {
                "id": "select_accounts_again",
                "operator": "SELECT",
                "inputs": [],
                "params": {"entity": "accounts", "fields": ["account_id"]},
                "outputs": [{"name": "account_id", "data_type": "integer"}],
            },
            {
                "id": "join_reversed",
                "operator": "JOIN",
                "inputs": ["join_once", "select_accounts_again"],
                "params": {
                    "relation": "account_orders",
                    "kind": "INNER",
                    "fields": [
                        {
                            "source": "left",
                            "field": "order_account_id",
                            "name": "order_account_id",
                        },
                        {
                            "source": "right",
                            "field": "account_id",
                            "name": "account_id",
                        },
                    ],
                },
                "outputs": [
                    {"name": "order_account_id", "data_type": "integer"},
                    {"name": "account_id", "data_type": "integer"},
                ],
            },
        ],
        "root": "join_reversed",
    }
    sqg = SemanticQueryGraph.model_validate(data)
    validate_sqg(sqg, ontology)


@pytest.mark.parametrize(
    ("join_kind", "metric_id", "group_projection", "metric_projection"),
    [
        (
            "LEFT",
            "order_amount",
            {"source": "left", "field": "account_id", "name": "group_key"},
            {"source": "right", "field": "amount", "name": "metric_value"},
        ),
        (
            "RIGHT",
            "account_credit",
            {"source": "right", "field": "account_id", "name": "group_key"},
            {"source": "left", "field": "credit", "name": "metric_value"},
        ),
    ],
)
def test_outer_join_nullability_reaches_grouped_aggregate(
    join_kind: str,
    metric_id: str,
    group_projection: dict,
    metric_projection: dict,
) -> None:
    ontology = Ontology.model_validate(
        {
            "contract_version": "ontology/v0",
            "ontology_id": "outer_join_demo",
            "version": "outer.v1",
            "entities": [
                {
                    "id": "accounts",
                    "fields": [
                        {
                            "id": "account_id",
                            "data_type": "integer",
                            "nullable": False,
                        },
                        {
                            "id": "credit",
                            "data_type": "decimal",
                            "nullable": False,
                        },
                    ],
                },
                {
                    "id": "orders",
                    "fields": [
                        {
                            "id": "account_id",
                            "data_type": "integer",
                            "nullable": False,
                        },
                        {
                            "id": "amount",
                            "data_type": "decimal",
                            "nullable": False,
                        },
                    ],
                },
            ],
            "metrics": [
                {
                    "id": "account_credit",
                    "entity": "accounts",
                    "field": "credit",
                    "aggregation": "SUM",
                },
                {
                    "id": "order_amount",
                    "entity": "orders",
                    "field": "amount",
                    "aggregation": "SUM",
                },
            ],
            "relations": [
                {
                    "id": "account_orders",
                    "left_entity": "accounts",
                    "right_entity": "orders",
                    "left_field": "account_id",
                    "right_field": "account_id",
                    "cardinality": "one_to_many",
                }
            ],
        }
    )
    data = {
        "contract_version": "sqg/v0",
        "query_id": f"{join_kind.lower()}_aggregate",
        "ontology_version": "outer.v1",
        "nodes": [
            {
                "id": "select_accounts",
                "operator": "SELECT",
                "inputs": [],
                "params": {
                    "entity": "accounts",
                    "fields": ["account_id", "credit"],
                },
                "outputs": [
                    {
                        "name": "account_id",
                        "data_type": "integer",
                        "nullable": False,
                    },
                    {
                        "name": "credit",
                        "data_type": "decimal",
                        "nullable": False,
                    },
                ],
            },
            {
                "id": "select_orders",
                "operator": "SELECT",
                "inputs": [],
                "params": {
                    "entity": "orders",
                    "fields": ["account_id", "amount"],
                },
                "outputs": [
                    {
                        "name": "account_id",
                        "data_type": "integer",
                        "nullable": False,
                    },
                    {
                        "name": "amount",
                        "data_type": "decimal",
                        "nullable": False,
                    },
                ],
            },
            {
                "id": "join_records",
                "operator": "JOIN",
                "inputs": ["select_accounts", "select_orders"],
                "params": {
                    "relation": "account_orders",
                    "kind": join_kind,
                    "fields": [group_projection, metric_projection],
                },
                "outputs": [
                    {
                        "name": "group_key",
                        "data_type": "integer",
                        "nullable": False,
                    },
                    {
                        "name": "metric_value",
                        "data_type": "decimal",
                        "nullable": True,
                    },
                ],
            },
            {
                "id": "aggregate_metric",
                "operator": "AGGREGATE",
                "inputs": ["join_records"],
                "params": {
                    "group_by": ["group_key"],
                    "measures": [
                        {
                            "metric": metric_id,
                            "source": "metric_value",
                            "name": "total",
                        }
                    ],
                },
                "outputs": [
                    {
                        "name": "group_key",
                        "data_type": "integer",
                        "nullable": False,
                    },
                    {"name": "total", "data_type": "decimal", "nullable": True},
                ],
            },
        ],
        "root": "aggregate_metric",
    }
    sqg = SemanticQueryGraph.model_validate(data)
    validate_sqg(sqg, ontology)

    invalid = copy.deepcopy(data)
    invalid["nodes"][-1]["outputs"][-1]["nullable"] = False
    with pytest.raises(SemanticValidationError, match="expected nullable=True"):
        validate_sqg(SemanticQueryGraph.model_validate(invalid), ontology)


@pytest.mark.parametrize(
    ("source_alias", "expected_nullable"),
    [("left_balance", False), ("right_balance", True)],
)
def test_measure_source_selects_one_self_join_occurrence(
    source_alias: str, expected_nullable: bool
) -> None:
    ontology = Ontology.model_validate(
        {
            "contract_version": "ontology/v0",
            "ontology_id": "self_join_demo",
            "version": "self.v1",
            "entities": [
                {
                    "id": "accounts",
                    "fields": [
                        {
                            "id": "account_id",
                            "data_type": "integer",
                            "nullable": False,
                        },
                        {
                            "id": "balance",
                            "data_type": "decimal",
                            "nullable": False,
                        },
                    ],
                }
            ],
            "metrics": [
                {
                    "id": "balance_total",
                    "entity": "accounts",
                    "field": "balance",
                    "aggregation": "SUM",
                }
            ],
            "relations": [
                {
                    "id": "account_pair",
                    "left_entity": "accounts",
                    "right_entity": "accounts",
                    "left_field": "account_id",
                    "right_field": "account_id",
                    "cardinality": "many_to_many",
                }
            ],
        }
    )
    select_outputs = [
        {"name": "account_id", "data_type": "integer", "nullable": False},
        {"name": "balance", "data_type": "decimal", "nullable": False},
    ]
    data = {
        "contract_version": "sqg/v0",
        "query_id": f"self_join_{source_alias}",
        "ontology_version": "self.v1",
        "nodes": [
            {
                "id": "select_left",
                "operator": "SELECT",
                "inputs": [],
                "params": {
                    "entity": "accounts",
                    "fields": ["account_id", "balance"],
                },
                "outputs": select_outputs,
            },
            {
                "id": "select_right",
                "operator": "SELECT",
                "inputs": [],
                "params": {
                    "entity": "accounts",
                    "fields": ["account_id", "balance"],
                },
                "outputs": select_outputs,
            },
            {
                "id": "join_accounts",
                "operator": "JOIN",
                "inputs": ["select_left", "select_right"],
                "params": {
                    "relation": "account_pair",
                    "kind": "LEFT",
                    "fields": [
                        {
                            "source": "left",
                            "field": "account_id",
                            "name": "left_id",
                        },
                        {
                            "source": "left",
                            "field": "balance",
                            "name": "left_balance",
                        },
                        {
                            "source": "right",
                            "field": "balance",
                            "name": "right_balance",
                        },
                    ],
                },
                "outputs": [
                    {
                        "name": "left_id",
                        "data_type": "integer",
                        "nullable": False,
                    },
                    {
                        "name": "left_balance",
                        "data_type": "decimal",
                        "nullable": False,
                    },
                    {
                        "name": "right_balance",
                        "data_type": "decimal",
                        "nullable": True,
                    },
                ],
            },
            {
                "id": "aggregate_balance",
                "operator": "AGGREGATE",
                "inputs": ["join_accounts"],
                "params": {
                    "group_by": ["left_id"],
                    "measures": [
                        {
                            "metric": "balance_total",
                            "source": source_alias,
                            "name": "total",
                        }
                    ],
                },
                "outputs": [
                    {
                        "name": "left_id",
                        "data_type": "integer",
                        "nullable": False,
                    },
                    {
                        "name": "total",
                        "data_type": "decimal",
                        "nullable": expected_nullable,
                    },
                ],
            },
        ],
        "root": "aggregate_balance",
    }
    sqg = SemanticQueryGraph.model_validate(data)
    validate_sqg(sqg, ontology)
