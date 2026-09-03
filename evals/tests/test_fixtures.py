from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import duckdb

from semantic_eval.evaluator import load_document


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "evals" / "fixtures" / "v0"
DATA = ROOT / "data" / "synthetic" / "generated"
BUILDER_PATH = ROOT / "evals" / "tools" / "build_fixtures.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("fixture_builder", BUILDER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def register_views(connection: duckdb.DuckDBPyConnection, data: Path = DATA) -> None:
    for table in ("dates", "regions", "products", "customers", "orders", "order_items"):
        connection.read_csv(str(data / f"{table}.csv"), header=True).create_view(table)


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
    register_views(connection)
    actual = connection.execute(
        """
        select r.region_name, round(sum(i.net_revenue-i.total_cost),2) gross_profit
        from orders o
        join order_items i using(order_id)
        join regions r using(region_id)
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


def test_cross_region_orders_distinguish_home_from_selling_region() -> None:
    _, cases = cases_by_id()
    connection = duckdb.connect()
    register_views(connection)
    cross_region_count = connection.execute(
        """
        select count(*)
        from orders o join customers c using(customer_id)
        where o.region_id <> c.home_region_id
        """
    ).fetchone()[0]
    assert cross_region_count > 0

    selling_region = connection.execute(
        """
        select r.region_name, round(sum(i.net_revenue),2) sales
        from orders o join order_items i using(order_id)
        join regions r using(region_id)
        where year(o.order_date)=2024
        group by r.region_name order by r.region_name
        """
    ).fetchall()
    home_region = connection.execute(
        """
        select r.region_name, round(sum(i.net_revenue),2) sales
        from orders o join order_items i using(order_id)
        join customers c using(customer_id)
        join regions r on c.home_region_id=r.region_id
        where year(o.order_date)=2024
        group by r.region_name order by r.region_name
        """
    ).fetchall()
    assert selling_region != home_region
    expected = cases["customer-home-region-sales"]["expected"]["result"]["rows"]
    assert home_region == [(row["region_name"], row["sales"]) for row in expected]


def test_fixture_builder_accepts_apostrophe_in_data_path(tmp_path: Path) -> None:
    data = tmp_path / "corpus's data"
    data.mkdir()
    for source in DATA.glob("*.csv"):
        shutil.copy2(source, data / source.name)
    connection = duckdb.connect()
    load_builder().register_data_views(connection, data)
    assert connection.execute("select count(*) from orders").fetchone()[0] > 0
