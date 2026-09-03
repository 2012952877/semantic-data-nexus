"""Build checked-in golden cases and static candidate fixtures."""

from __future__ import annotations

import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "evals" / "fixtures" / "v0"
DATA = ROOT / "data" / "synthetic" / "generated"
EVAL_CLOCK = "2025-04-15T09:00:00+08:00"
TABLES = ("dates", "regions", "products", "customers", "orders", "order_items")


def serializable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def rows(connection: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    result = connection.execute(sql)
    columns = [column[0] for column in result.description]
    return [
        {column: serializable(value) for column, value in zip(columns, row, strict=True)}
        for row in result.fetchall()
    ]


def edges(operators: list[str]) -> list[list[str]]:
    return [
        [f"{index}:{operators[index]}", f"{index + 1}:{operators[index + 1]}"]
        for index in range(len(operators) - 1)
    ]


def plan_shape(operators: list[str]) -> dict[str, Any]:
    node_ids = [f"{index}:{operator}" for index, operator in enumerate(operators)]
    nodes = []
    for index, (node_id, operator) in enumerate(zip(node_ids, operators, strict=True)):
        dependencies = [] if index == 0 else [node_ids[index - 1]]
        nodes.append(
            {
                "id": node_id,
                "operator": operator,
                "depends_on": dependencies,
                "inputs": [] if index == 0 else ["rows"],
                "outputs": ["rows"],
            }
        )
    return {"operators": operators, "edges": edges(operators), "nodes": nodes}


def register_data_views(
    connection: duckdb.DuckDBPyConnection,
    data_directory: Path = DATA,
) -> None:
    for table in TABLES:
        connection.read_csv(str(data_directory / f"{table}.csv"), header=True).create_view(
            table
        )


def success_case(
    connection: duckdb.DuckDBPyConnection,
    *,
    case_id: str,
    question: str,
    sql: str,
    entities: list[str],
    fields: list[str],
    metrics: list[str],
    relations: list[str],
    operators: list[str],
    schema: list[dict[str, str]],
    grain: list[str],
    order_by: list[str],
    time_range: dict[str, Any],
    member_normalization: dict[str, str] | None = None,
    context: str | None = None,
    behavior_class: str = "success",
    governance: dict[str, Any] | None = None,
    tolerance: float = 0.01,
) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": case_id,
        "question_zh": question,
        "expected": {
            "semantic": {
                "entities": entities,
                "fields": fields,
                "metrics": metrics,
                "relations": relations,
            },
            "time_range": time_range,
            "member_normalization": member_normalization or {},
            "plan": plan_shape(operators),
            "result": {
                "schema": schema,
                "grain": grain,
                "order_by": order_by,
                "rows": rows(connection, sql),
                "tolerance": tolerance,
            },
            "behavior": {"class": behavior_class},
            "governance": governance or {"decision": "allow"},
            "observability": {"lineage_required": True},
        },
    }
    if context:
        case["clarifying_context"] = context
    return case


def diagnostic_case(
    *,
    case_id: str,
    question: str,
    behavior_class: str,
    diagnostic_code: str,
    entities: list[str] | None = None,
    fields: list[str] | None = None,
    metrics: list[str] | None = None,
    member_normalization: dict[str, str] | None = None,
    governance: dict[str, Any] | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": case_id,
        "question_zh": question,
        "expected": {
            "semantic": {
                "entities": entities or [],
                "fields": fields or [],
                "metrics": metrics or [],
                "relations": [],
            },
            "time_range": {},
            "member_normalization": member_normalization or {},
            "plan": plan_shape([]),
            "result": {
                "schema": [],
                "grain": [],
                "order_by": [],
                "rows": [],
                "tolerance": 0.0,
            },
            "behavior": {
                "class": behavior_class,
                "diagnostic_code": diagnostic_code,
            },
            "governance": governance or {"decision": "not_applicable"},
            "observability": {"lineage_required": False},
        },
    }
    if context:
        case["clarifying_context"] = context
    return case


def main() -> None:
    connection = duckdb.connect()
    register_data_views(connection)

    region_profit_sql = """
        select r.region_name, round(sum(i.net_revenue - i.total_cost), 2) as gross_profit
        from orders o join order_items i using(order_id) join regions r using(region_id)
        where o.order_date >= date '2024-01-01' and o.order_date < date '2024-04-01'
        group by r.region_name order by gross_profit desc, r.region_name
    """
    cases = [
        success_case(
            connection,
            case_id="regional-quarterly-gross-profit",
            question="2024年第一季度各区域毛利润是多少？",
            sql=region_profit_sql,
            entities=["order", "order_item", "region"],
            fields=["region.region_name", "order.order_date"],
            metrics=["gross_profit"],
            relations=["item_order", "order_region"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "SORT", "PROJECT"],
            schema=[
                {"name": "region_name", "type": "string"},
                {"name": "gross_profit", "type": "decimal"},
            ],
            grain=["region.region_name"],
            order_by=["gross_profit desc", "region_name asc"],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2024-04-01",
                "source": "explicit_quarter",
            },
        ),
        success_case(
            connection,
            case_id="regional-quarterly-gross-profit-q4",
            question="按地区列出2024年第四季度毛利。",
            sql=region_profit_sql.replace("2024-01-01", "2024-10-01").replace(
                "2024-04-01", "2025-01-01"
            ),
            entities=["order", "order_item", "region"],
            fields=["region.region_name", "order.order_date"],
            metrics=["gross_profit"],
            relations=["item_order", "order_region"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "SORT", "PROJECT"],
            schema=[
                {"name": "region_name", "type": "string"},
                {"name": "gross_profit", "type": "decimal"},
            ],
            grain=["region.region_name"],
            order_by=["gross_profit desc", "region_name asc"],
            time_range={
                "start": "2024-10-01",
                "end_exclusive": "2025-01-01",
                "source": "explicit_quarter",
            },
        ),
        success_case(
            connection,
            case_id="monthly-regional-sales-comparison",
            question="对比2024年第一季度每月各区域销售额，并计算各月总额。",
            sql="""
                with base as (
                  select strftime(o.order_date, '%Y-%m') as month, r.region_code,
                         sum(i.net_revenue) as sales
                  from orders o join order_items i using(order_id)
                  join regions r using(region_id)
                  where o.order_date >= date '2024-01-01'
                    and o.order_date < date '2024-04-01'
                  group by month, r.region_code
                )
                select month,
                  round(sum(case when region_code='north' then sales else 0 end),2) north_sales,
                  round(sum(case when region_code='south' then sales else 0 end),2) south_sales,
                  round(sum(case when region_code='east' then sales else 0 end),2) east_sales,
                  round(sum(case when region_code='west' then sales else 0 end),2) west_sales,
                  round(sum(sales),2) total_sales
                from base group by month order by month
            """,
            entities=["date", "order", "order_item", "region"],
            fields=["date.month", "region.region_code", "order.order_date"],
            metrics=["sales"],
            relations=["item_order", "order_region", "order_date"],
            operators=[
                "SCAN",
                "JOIN",
                "FILTER",
                "AGGREGATE",
                "PIVOT",
                "DERIVE",
                "PROJECT",
            ],
            schema=[
                {"name": "month", "type": "string"},
                {"name": "north_sales", "type": "decimal"},
                {"name": "south_sales", "type": "decimal"},
                {"name": "east_sales", "type": "decimal"},
                {"name": "west_sales", "type": "decimal"},
                {"name": "total_sales", "type": "decimal"},
            ],
            grain=["date.month"],
            order_by=["month asc"],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2024-04-01",
                "source": "explicit_quarter",
            },
        ),
        success_case(
            connection,
            case_id="top-three-products-by-sales",
            question="2024年销售额最高的三个产品是什么？",
            sql="""
                select p.product_id, p.product_name, round(sum(i.net_revenue),2) sales
                from orders o join order_items i using(order_id)
                join products p using(product_id)
                where o.order_date >= date '2024-01-01'
                  and o.order_date < date '2025-01-01'
                group by p.product_id, p.product_name
                order by sales desc, p.product_id limit 3
            """,
            entities=["order", "order_item", "product"],
            fields=["product.product_id", "product.product_name", "order.order_date"],
            metrics=["sales"],
            relations=["item_order", "item_product"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "TOP_N", "PROJECT"],
            schema=[
                {"name": "product_id", "type": "string"},
                {"name": "product_name", "type": "string"},
                {"name": "sales", "type": "decimal"},
            ],
            grain=["product.product_id", "product.product_name"],
            order_by=["sales desc", "product_id asc"],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2025-01-01",
                "source": "explicit_year",
            },
        ),
        success_case(
            connection,
            case_id="category-sales-and-cost",
            question="2024年各产品类别的销售额和成本。",
            sql="""
                select p.category, round(sum(i.net_revenue),2) sales,
                       round(sum(i.total_cost),2) as "cost"
                from orders o join order_items i using(order_id)
                join products p using(product_id)
                where year(o.order_date)=2024
                group by p.category order by p.category
            """,
            entities=["order", "order_item", "product"],
            fields=["product.category", "order.order_date"],
            metrics=["sales", "cost"],
            relations=["item_order", "item_product"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "PROJECT"],
            schema=[
                {"name": "category", "type": "string"},
                {"name": "sales", "type": "decimal"},
                {"name": "cost", "type": "decimal"},
            ],
            grain=["product.category"],
            order_by=["category asc"],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2025-01-01",
                "source": "explicit_year",
            },
        ),
        success_case(
            connection,
            case_id="enterprise-segment-order-count",
            question="2024年企业客户每个区域的订单数。",
            sql="""
                select r.region_name, count(distinct o.order_id) order_count
                from orders o join customers c using(customer_id)
                join regions r using(region_id)
                where c.segment='enterprise' and year(o.order_date)=2024
                group by r.region_name order by r.region_name
            """,
            entities=["order", "customer", "region"],
            fields=["customer.segment", "region.region_name", "order.order_date"],
            metrics=["order_count"],
            relations=["order_customer", "order_region"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "PROJECT"],
            schema=[
                {"name": "region_name", "type": "string"},
                {"name": "order_count", "type": "integer"},
            ],
            grain=["region.region_name"],
            order_by=["region_name asc"],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2025-01-01",
                "source": "explicit_year",
            },
            member_normalization={"企业客户": "enterprise"},
            governance={"decision": "allow", "applied_policy": "customer_aggregate_only"},
        ),
        success_case(
            connection,
            case_id="customer-home-region-sales",
            question="2024年按客户归属区域统计销售额。",
            sql="""
                select r.region_name, round(sum(i.net_revenue),2) sales
                from orders o join order_items i using(order_id)
                join customers c using(customer_id)
                join regions r on c.home_region_id=r.region_id
                where year(o.order_date)=2024
                group by r.region_name order by r.region_name
            """,
            entities=["customer", "order", "order_item", "region"],
            fields=[
                "customer.home_region_id",
                "region.region_name",
                "order.order_date",
            ],
            metrics=["sales"],
            relations=["item_order", "order_customer", "customer_home_region"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "SORT", "PROJECT"],
            schema=[
                {"name": "region_name", "type": "string"},
                {"name": "sales", "type": "decimal"},
            ],
            grain=["region.region_name"],
            order_by=["region_name asc"],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2025-01-01",
                "source": "explicit_year",
            },
            governance={"decision": "allow", "applied_policy": "customer_aggregate_only"},
        ),
        success_case(
            connection,
            case_id="explicit-date-north-sales",
            question="2024年3月1日至3月15日北区销售额。",
            sql="""
                select round(sum(i.net_revenue),2) sales
                from orders o join order_items i using(order_id)
                where o.region_id='R01' and o.order_date >= date '2024-03-01'
                  and o.order_date < date '2024-03-16'
            """,
            entities=["order", "order_item", "region"],
            fields=["order.order_date", "region.region_id"],
            metrics=["sales"],
            relations=["item_order", "order_region"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "PROJECT"],
            schema=[{"name": "sales", "type": "decimal"}],
            grain=[],
            order_by=[],
            time_range={
                "start": "2024-03-01",
                "end_exclusive": "2024-03-16",
                "source": "explicit_dates",
            },
            member_normalization={"北区": "R01"},
        ),
        success_case(
            connection,
            case_id="relative-last-quarter-sales",
            question="上季度各区域销售额。",
            context=f"评测时钟固定为 {EVAL_CLOCK}",
            sql="""
                select r.region_name, round(sum(i.net_revenue),2) sales
                from orders o join order_items i using(order_id)
                join regions r using(region_id)
                where o.order_date >= date '2025-01-01'
                  and o.order_date < date '2025-04-01'
                group by r.region_name order by r.region_name
            """,
            entities=["order", "order_item", "region"],
            fields=["region.region_name", "order.order_date"],
            metrics=["sales"],
            relations=["item_order", "order_region"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "PROJECT"],
            schema=[
                {"name": "region_name", "type": "string"},
                {"name": "sales", "type": "decimal"},
            ],
            grain=["region.region_name"],
            order_by=["region_name asc"],
            time_range={
                "start": "2025-01-01",
                "end_exclusive": "2025-04-01",
                "source": "relative_last_quarter",
                "evaluation_clock": EVAL_CLOCK,
            },
        ),
        diagnostic_case(
            case_id="ambiguous-product-name",
            question="晨星标准版的销售额是多少？",
            behavior_class="clarification",
            diagnostic_code="AMBIGUOUS_MEMBER",
            entities=["product", "order_item"],
            fields=["product.product_name"],
            metrics=["sales"],
            context="该名称对应设备 P01 和配件 P02，必须请求用户选择。",
        ),
        success_case(
            connection,
            case_id="zero-denominator-gross-margin",
            question="远山试用包2024年的毛利率。",
            sql="""
                select p.product_id,
                  case when sum(i.net_revenue)=0 then null
                       else round(sum(i.net_revenue-i.total_cost)/sum(i.net_revenue),4)
                  end gross_margin
                from orders o join order_items i using(order_id)
                join products p using(product_id)
                where p.product_id='P08' and year(o.order_date)=2024
                group by p.product_id
            """,
            entities=["order", "order_item", "product"],
            fields=["product.product_id", "order.order_date"],
            metrics=["gross_margin"],
            relations=["item_order", "item_product"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "DERIVE", "PROJECT"],
            schema=[
                {"name": "product_id", "type": "string"},
                {"name": "gross_margin", "type": "decimal_nullable"},
            ],
            grain=["product.product_id"],
            order_by=[],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2025-01-01",
                "source": "explicit_year",
            },
            member_normalization={"远山试用包": "P08"},
            tolerance=0.0001,
        ),
        success_case(
            connection,
            case_id="valid-no-data-period",
            question="2025年第二季度各区域销售额。",
            sql="""
                select r.region_name, round(sum(i.net_revenue),2) sales
                from orders o join order_items i using(order_id)
                join regions r using(region_id)
                where o.order_date >= date '2025-04-01'
                  and o.order_date < date '2025-07-01'
                group by r.region_name order by r.region_name
            """,
            entities=["order", "order_item", "region"],
            fields=["region.region_name", "order.order_date"],
            metrics=["sales"],
            relations=["item_order", "order_region"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "PROJECT"],
            schema=[
                {"name": "region_name", "type": "string"},
                {"name": "sales", "type": "decimal"},
            ],
            grain=["region.region_name"],
            order_by=["region_name asc"],
            time_range={
                "start": "2025-04-01",
                "end_exclusive": "2025-07-01",
                "source": "explicit_quarter",
            },
            behavior_class="no-data",
        ),
        diagnostic_case(
            case_id="customer-row-policy-denied",
            question="列出所有客户编号及其订单明细。",
            behavior_class="policy-denied",
            diagnostic_code="AGGREGATE_ONLY_ENTITY",
            entities=["customer", "order"],
            fields=["customer.customer_id", "order.order_id"],
            governance={
                "decision": "deny",
                "applied_policy": "customer_aggregate_only",
            },
        ),
        diagnostic_case(
            case_id="unknown-metric",
            question="按区域计算2024年的品牌热度。",
            behavior_class="invalid-concept",
            diagnostic_code="UNKNOWN_METRIC",
            entities=["region"],
            fields=["region.region_name"],
        ),
        diagnostic_case(
            case_id="unknown-entity",
            question="按仓库统计2024年销售额。",
            behavior_class="invalid-concept",
            diagnostic_code="UNKNOWN_ENTITY",
            metrics=["sales"],
        ),
        diagnostic_case(
            case_id="timeout-fixture",
            question="执行超时评测夹具。",
            behavior_class="timeout",
            diagnostic_code="EXECUTION_TIMEOUT",
        ),
        diagnostic_case(
            case_id="cancel-fixture",
            question="执行取消评测夹具。",
            behavior_class="cancelled",
            diagnostic_code="EXECUTION_CANCELLED",
        ),
        success_case(
            connection,
            case_id="monthly-order-count",
            question="2024年每月订单数。",
            sql="""
                select strftime(order_date,'%Y-%m') as "month",
                       count(distinct order_id) order_count
                from orders where year(order_date)=2024
                group by month order by month
            """,
            entities=["order", "date"],
            fields=["date.month", "order.order_date"],
            metrics=["order_count"],
            relations=["order_date"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "PROJECT"],
            schema=[
                {"name": "month", "type": "string"},
                {"name": "order_count", "type": "integer"},
            ],
            grain=["date.month"],
            order_by=["month asc"],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2025-01-01",
                "source": "explicit_year",
            },
        ),
        success_case(
            connection,
            case_id="returns-by-region",
            question="2024年各区域退货订单数。",
            sql="""
                select r.region_name, count(distinct o.order_id) return_count
                from orders o join regions r using(region_id)
                where o.status='returned' and year(o.order_date)=2024
                group by r.region_name order by r.region_name
            """,
            entities=["order", "region"],
            fields=["order.status", "region.region_name", "order.order_date"],
            metrics=["return_count"],
            relations=["order_region"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "PROJECT"],
            schema=[
                {"name": "region_name", "type": "string"},
                {"name": "return_count", "type": "integer"},
            ],
            grain=["region.region_name"],
            order_by=["region_name asc"],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2025-01-01",
                "source": "explicit_year",
            },
            member_normalization={"退货": "returned"},
        ),
        success_case(
            connection,
            case_id="promotion-sales",
            question="2024年使用促销码的销售额。",
            sql="""
                select round(sum(i.net_revenue),2) sales
                from orders o join order_items i using(order_id)
                where o.promotion_code is not null and year(o.order_date)=2024
            """,
            entities=["order", "order_item"],
            fields=["order.promotion_code", "order.order_date"],
            metrics=["sales"],
            relations=["item_order"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "PROJECT"],
            schema=[{"name": "sales", "type": "decimal"}],
            grain=[],
            order_by=[],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2025-01-01",
                "source": "explicit_year",
            },
        ),
        success_case(
            connection,
            case_id="regional-gross-margin",
            question="2024年各区域毛利率。",
            sql="""
                select r.region_name,
                  round(sum(i.net_revenue-i.total_cost)/nullif(sum(i.net_revenue),0),4)
                    gross_margin
                from orders o join order_items i using(order_id)
                join regions r using(region_id)
                where year(o.order_date)=2024
                group by r.region_name order by r.region_name
            """,
            entities=["order", "order_item", "region"],
            fields=["region.region_name", "order.order_date"],
            metrics=["gross_margin"],
            relations=["item_order", "order_region"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "DERIVE", "PROJECT"],
            schema=[
                {"name": "region_name", "type": "string"},
                {"name": "gross_margin", "type": "decimal"},
            ],
            grain=["region.region_name"],
            order_by=["region_name asc"],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2025-01-01",
                "source": "explicit_year",
            },
            tolerance=0.0001,
        ),
        success_case(
            connection,
            case_id="year-over-year-sales",
            question="比较2024年和2025年第一季度销售额。",
            sql="""
                select year(o.order_date) as "year", round(sum(i.net_revenue),2) sales
                from orders o join order_items i using(order_id)
                where (o.order_date>=date '2024-01-01' and o.order_date<date '2024-04-01')
                   or (o.order_date>=date '2025-01-01' and o.order_date<date '2025-04-01')
                group by year order by year
            """,
            entities=["order", "order_item", "date"],
            fields=["date.year", "date.quarter", "order.order_date"],
            metrics=["sales"],
            relations=["item_order", "order_date"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "PROJECT"],
            schema=[
                {"name": "year", "type": "integer"},
                {"name": "sales", "type": "decimal"},
            ],
            grain=["date.year"],
            order_by=["year asc"],
            time_range={
                "intervals": [
                    {"start": "2024-01-01", "end_exclusive": "2024-04-01"},
                    {"start": "2025-01-01", "end_exclusive": "2025-04-01"},
                ],
                "source": "explicit_comparison",
            },
        ),
        success_case(
            connection,
            case_id="west-product-profit",
            question="西区2024年各产品毛利润。",
            sql="""
                select p.product_id, p.product_name,
                       round(sum(i.net_revenue-i.total_cost),2) gross_profit
                from orders o join order_items i using(order_id)
                join products p using(product_id)
                where o.region_id='R04' and year(o.order_date)=2024
                group by p.product_id,p.product_name order by p.product_id
            """,
            entities=["order", "order_item", "product", "region"],
            fields=[
                "product.product_id",
                "product.product_name",
                "region.region_id",
                "order.order_date",
            ],
            metrics=["gross_profit"],
            relations=["item_order", "item_product", "order_region"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "PROJECT"],
            schema=[
                {"name": "product_id", "type": "string"},
                {"name": "product_name", "type": "string"},
                {"name": "gross_profit", "type": "decimal"},
            ],
            grain=["product.product_id", "product.product_name"],
            order_by=["product_id asc"],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2025-01-01",
                "source": "explicit_year",
            },
            member_normalization={"西区": "R04"},
        ),
        success_case(
            connection,
            case_id="orders-without-promotion",
            question="2024年未使用促销码的订单数。",
            sql="""
                select count(distinct order_id) order_count
                from orders where promotion_code is null and year(order_date)=2024
            """,
            entities=["order"],
            fields=["order.promotion_code", "order.order_date"],
            metrics=["order_count"],
            relations=[],
            operators=["SCAN", "FILTER", "AGGREGATE", "PROJECT"],
            schema=[{"name": "order_count", "type": "integer"}],
            grain=[],
            order_by=[],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2025-01-01",
                "source": "explicit_year",
            },
        ),
        success_case(
            connection,
            case_id="south-monthly-profit",
            question="南港区2024年每月毛利润。",
            sql="""
                select strftime(o.order_date,'%Y-%m') as "month",
                       round(sum(i.net_revenue-i.total_cost),2) gross_profit
                from orders o join order_items i using(order_id)
                where o.region_id='R02' and year(o.order_date)=2024
                group by month order by month
            """,
            entities=["order", "order_item", "region", "date"],
            fields=["date.month", "region.region_id", "order.order_date"],
            metrics=["gross_profit"],
            relations=["item_order", "order_region", "order_date"],
            operators=["SCAN", "JOIN", "FILTER", "AGGREGATE", "PROJECT"],
            schema=[
                {"name": "month", "type": "string"},
                {"name": "gross_profit", "type": "decimal"},
            ],
            grain=["date.month"],
            order_by=["month asc"],
            time_range={
                "start": "2024-01-01",
                "end_exclusive": "2025-01-01",
                "source": "explicit_year",
            },
            member_normalization={"南港区": "R02"},
        ),
        diagnostic_case(
            case_id="invalid-plan-duplicate-node",
            question="验证重复计划节点能被拒绝。",
            behavior_class="invalid-plan",
            diagnostic_code="DUPLICATE_NODE_ID",
        ),
        diagnostic_case(
            case_id="invalid-plan-missing-dependency",
            question="验证缺失依赖的计划能被拒绝。",
            behavior_class="invalid-plan",
            diagnostic_code="MISSING_DEPENDENCY",
        ),
        diagnostic_case(
            case_id="invalid-plan-column-flow",
            question="验证列流不完整的计划能被拒绝。",
            behavior_class="invalid-plan",
            diagnostic_code="INVALID_COLUMN_FLOW",
        ),
    ]

    suite = {
        "suite_version": "golden-sales-v0",
        "structure_status": "project-independent-evaluation-draft",
        "evaluation_clock": EVAL_CLOCK,
        "weights": {
            "semantic": 30,
            "plan": 25,
            "execution_result": 25,
            "governance": 10,
            "observability": 10,
        },
        "cases": cases,
    }
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "golden_cases.json").write_text(
        json.dumps(suite, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    passing_cases: dict[str, Any] = {}
    for case in cases:
        expected = copy.deepcopy(case["expected"])
        actual = {
            key: expected[key]
            for key in (
                "semantic",
                "time_range",
                "member_normalization",
                "plan",
                "result",
                "behavior",
                "governance",
            )
        }
        if expected["observability"]["lineage_required"]:
            actual["lineage"] = {
                "entities": expected["semantic"]["entities"],
                "fields": expected["semantic"]["fields"],
                "artifact": f"reference:{case['id']}",
            }
        passing_cases[case["id"]] = actual

    passing = {"artifact_version": "candidate-v0", "cases": passing_cases}
    (FIXTURES / "candidates").mkdir(exist_ok=True)
    (FIXTURES / "candidates" / "passing.json").write_text(
        json.dumps(passing, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    failing = copy.deepcopy(passing)
    failing["cases"]["regional-quarterly-gross-profit"]["semantic"]["metrics"] = [
        "sales"
    ]
    failing["cases"]["monthly-regional-sales-comparison"]["plan"]["operators"].remove(
        "PIVOT"
    )
    failing["cases"]["top-three-products-by-sales"]["result"]["rows"][0]["sales"] += 5
    failing["cases"]["relative-last-quarter-sales"]["time_range"]["start"] = "2024-10-01"
    failing["cases"]["customer-row-policy-denied"]["governance"]["decision"] = "allow"
    failing["cases"]["regional-gross-margin"].pop("lineage")
    failing["cases"]["invalid-plan-duplicate-node"]["plan"] = {
        "operators": [],
        "edges": [],
        "nodes": [
            {"id": "n1", "outputs": ["sales"]},
            {"id": "n1", "outputs": ["profit"]},
        ],
    }
    (FIXTURES / "candidates" / "failing.json").write_text(
        json.dumps(failing, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    invalid_plans = {
        "artifact_version": "plan-fixtures-v0",
        "fixtures": [
            {
                "id": "duplicate-node",
                "expected_diagnostic": "DUPLICATE_NODE_ID",
                "plan": {
                    "operators": ["SCAN", "PROJECT"],
                    "edges": [["n1", "n1"]],
                    "nodes": [
                        {
                            "id": "n1",
                            "operator": "SCAN",
                            "depends_on": [],
                            "inputs": [],
                            "outputs": ["sales"],
                        },
                        {
                            "id": "n1",
                            "operator": "PROJECT",
                            "depends_on": ["n1"],
                            "inputs": ["sales"],
                            "outputs": ["sales"],
                        },
                    ]
                },
            },
            {
                "id": "missing-dependency",
                "expected_diagnostic": "MISSING_DEPENDENCY",
                "plan": {
                    "operators": ["AGGREGATE"],
                    "edges": [["missing_scan", "aggregate"]],
                    "nodes": [
                        {
                            "id": "aggregate",
                            "operator": "AGGREGATE",
                            "depends_on": ["missing_scan"],
                            "inputs": ["rows"],
                            "outputs": ["sales"],
                        }
                    ]
                },
            },
            {
                "id": "invalid-column-flow",
                "expected_diagnostic": "INVALID_COLUMN_FLOW",
                "plan": {
                    "operators": ["SCAN", "DERIVE"],
                    "edges": [["scan", "derive"]],
                    "nodes": [
                        {
                            "id": "scan",
                            "operator": "SCAN",
                            "depends_on": [],
                            "inputs": [],
                            "outputs": ["sales"],
                        },
                        {
                            "id": "derive",
                            "operator": "DERIVE",
                            "depends_on": ["scan"],
                            "inputs": ["gross_profit"],
                            "outputs": ["gross_margin"],
                        },
                    ]
                },
            },
            {
                "id": "dependency-cycle",
                "expected_diagnostic": "DEPENDENCY_CYCLE",
                "plan": {
                    "operators": ["DERIVE", "PROJECT"],
                    "edges": [["derive", "project"], ["project", "derive"]],
                    "nodes": [
                        {
                            "id": "derive",
                            "operator": "DERIVE",
                            "depends_on": ["project"],
                            "inputs": ["rows"],
                            "outputs": ["rows"],
                        },
                        {
                            "id": "project",
                            "operator": "PROJECT",
                            "depends_on": ["derive"],
                            "inputs": ["rows"],
                            "outputs": ["rows"],
                        },
                    ],
                },
            },
            {
                "id": "empty-node-graph",
                "expected_diagnostic": "EMPTY_NODE_GRAPH",
                "plan": {"operators": ["SCAN"], "edges": [], "nodes": []},
            },
            {
                "id": "missing-node-id",
                "expected_diagnostic": "MISSING_NODE_ID",
                "plan": {
                    "operators": ["SCAN"],
                    "edges": [],
                    "nodes": [
                        {
                            "id": "",
                            "operator": "SCAN",
                            "depends_on": [],
                            "inputs": [],
                            "outputs": ["rows"],
                        }
                    ],
                },
            },
            {
                "id": "disconnected-nodes",
                "expected_diagnostic": "DISCONNECTED_GRAPH",
                "plan": {
                    "operators": ["SCAN", "SCAN"],
                    "edges": [],
                    "nodes": [
                        {
                            "id": "left",
                            "operator": "SCAN",
                            "depends_on": [],
                            "inputs": [],
                            "outputs": ["left_id"],
                        },
                        {
                            "id": "right",
                            "operator": "SCAN",
                            "depends_on": [],
                            "inputs": [],
                            "outputs": ["right_id"],
                        },
                    ],
                },
            },
            {
                "id": "malformed-node-fields",
                "expected_diagnostic": "MALFORMED_NODE_FIELDS",
                "plan": {
                    "operators": ["SCAN", "PROJECT"],
                    "edges": [["scan", "project"]],
                    "nodes": [
                        {
                            "id": "scan",
                            "operator": "SCAN",
                            "depends_on": [],
                            "inputs": [],
                            "outputs": None,
                        },
                        {
                            "id": "project",
                            "operator": "PROJECT",
                            "depends_on": ["scan"],
                            "inputs": [[]],
                            "outputs": ["rows"],
                        },
                    ],
                },
            },
        ],
    }
    (FIXTURES / "candidates" / "invalid_plans.json").write_text(
        json.dumps(invalid_plans, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
