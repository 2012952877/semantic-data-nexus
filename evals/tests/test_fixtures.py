from __future__ import annotations

from pathlib import Path

import duckdb

from semantic_eval.evaluator import load_document


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "evals" / "fixtures" / "v0"
DATA = ROOT / "data" / "synthetic" / "generated"


def cases_by_id():
    suite = load_document(FIXTURES / "golden_cases.json")
    return suite, {case["id"]: case for case in suite["cases"]}


def test_suite_has_required_coverage_and_fixed_clock() -> None:
    suite, cases = cases_by_id()
    assert len(cases) >= 20
    assert suite["evaluation_clock"] == "2025-04-15T09:00:00+08:00"
    behavior_classes = {
        case["expected"]["behavior"]["class"] for case in cases.values()
    }
    assert {
        "success",
        "clarification",
        "no-data",
        "policy-denied",
        "invalid-concept",
        "timeout",
        "cancelled",
        "invalid-plan",
    } <= behavior_classes
    operators = cases["monthly-regional-sales-comparison"]["expected"]["plan"][
        "operators"
    ]
    pivot = operators.index("PIVOT")
    assert operators[pivot : pivot + 3] == ["PIVOT", "DERIVE", "PROJECT"]


def test_reference_profit_answer_matches_duckdb() -> None:
    _, cases = cases_by_id()
    connection = duckdb.connect()
    orders = (DATA / "orders.csv").as_posix()
    items = (DATA / "order_items.csv").as_posix()
    regions = (DATA / "regions.csv").as_posix()
    actual = connection.execute(
        f"""
        select r.region_name, round(sum(i.net_revenue-i.total_cost),2) gross_profit
        from read_csv_auto('{orders}') o
        join read_csv_auto('{items}') i using(order_id)
        join read_csv_auto('{regions}') r using(region_id)
        where o.order_date >= date '2024-01-01'
          and o.order_date < date '2024-04-01'
        group by r.region_name order by gross_profit desc, r.region_name
        """
    ).fetchall()
    expected = cases["regional-quarterly-gross-profit"]["expected"]["result"]["rows"]
    assert actual == [
        (row["region_name"], row["gross_profit"]) for row in expected
    ]


def test_no_data_and_zero_denominator_answers() -> None:
    _, cases = cases_by_id()
    no_data = cases["valid-no-data-period"]["expected"]
    zero = cases["zero-denominator-gross-margin"]["expected"]
    assert no_data["behavior"]["class"] == "no-data"
    assert no_data["result"]["rows"] == []
    assert zero["result"]["rows"] == [{"product_id": "P08", "gross_margin": None}]


def test_policy_and_ambiguity_are_explicit() -> None:
    _, cases = cases_by_id()
    denied = cases["customer-row-policy-denied"]["expected"]
    ambiguous = cases["ambiguous-product-name"]["expected"]
    assert denied["governance"] == {
        "decision": "deny",
        "applied_policy": "customer_aggregate_only",
    }
    assert denied["behavior"]["class"] == "policy-denied"
    assert ambiguous["behavior"]["diagnostic_code"] == "AMBIGUOUS_MEMBER"
