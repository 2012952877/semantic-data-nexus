"""Generate the deterministic clean-room sales corpus."""

from __future__ import annotations

import argparse
import csv
import hashlib
import random
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

SEED = 20240917
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "generated"

REGIONS = [
    ("R01", "北辰区", "north"),
    ("R02", "南港区", "south"),
    ("R03", "东湖区", "east"),
    ("R04", "西岭区", "west"),
]

PRODUCTS = [
    ("P01", "晨星标准版", "设备", "120.00", "72.00"),
    ("P02", "晨星标准版", "配件", "45.00", "18.00"),
    ("P03", "云帆增强版", "设备", "180.00", "108.00"),
    ("P04", "云帆轻量版", "设备", "90.00", "54.00"),
    ("P05", "青岚套件", "套件", "240.00", "144.00"),
    ("P06", "赤霞模块", "配件", "75.00", "30.00"),
    ("P07", "星河服务包", "服务", "60.00", "24.00"),
    ("P08", "远山试用包", "服务", "0.00", "0.00"),
]

CUSTOMERS = [
    ("C001", "样本客户-001", "R01", "standard"),
    ("C002", "样本客户-002", "R01", "enterprise"),
    ("C003", "样本客户-003", "R02", "standard"),
    ("C004", "样本客户-004", "R02", "enterprise"),
    ("C005", "样本客户-005", "R03", "standard"),
    ("C006", "样本客户-006", "R03", "enterprise"),
    ("C007", "样本客户-007", "R04", "standard"),
    ("C008", "样本客户-008", "R04", "enterprise"),
    ("C009", "样本客户-009", "R01", "standard"),
    ("C010", "样本客户-010", "R03", "standard"),
    ("C011", "样本客户-011", "R04", "standard"),
    ("C012", "样本客户-012", "R02", "standard"),
]


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def canonical_csv_bytes(path: Path) -> bytes:
    """Normalize checkout line endings before deterministic corpus hashing."""
    return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def corpus_digest(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(canonical_csv_bytes(path)).hexdigest()
        for path in sorted(directory.glob("*.csv"))
    }


def generate(output: Path = DEFAULT_OUTPUT) -> None:
    rng = random.Random(SEED)
    output.mkdir(parents=True, exist_ok=True)

    start = date(2024, 1, 1)
    end = date(2025, 6, 30)
    dates: list[dict[str, object]] = []
    current = start
    while current <= end:
        dates.append(
            {
                "date_id": current.isoformat(),
                "year": current.year,
                "quarter": f"{current.year}-Q{((current.month - 1) // 3) + 1}",
                "month": f"{current.year}-{current.month:02d}",
                "day_of_week": current.isoweekday(),
            }
        )
        current += timedelta(days=1)

    regions = [
        {"region_id": region_id, "region_name": name, "region_code": code}
        for region_id, name, code in REGIONS
    ]
    products = [
        {
            "product_id": product_id,
            "product_name": name,
            "category": category,
            "unit_price": price,
            "unit_cost": cost,
        }
        for product_id, name, category, price, cost in PRODUCTS
    ]
    customers = [
        {
            "customer_id": customer_id,
            "customer_label": label,
            "home_region_id": region_id,
            "segment": segment,
        }
        for customer_id, label, region_id, segment in CUSTOMERS
    ]

    orders: list[dict[str, object]] = []
    items: list[dict[str, object]] = []
    order_number = 1
    item_number = 1
    product_prices = {
        product_id: (Decimal(price), Decimal(cost))
        for product_id, _, _, price, cost in PRODUCTS
    }

    for year, month in (
        (year, month)
        for year in (2024, 2025)
        for month in range(1, 13)
        if date(year, month, 1) <= end
    ):
        # 2025-Q2 is deliberately a valid calendar period with no orders.
        if year == 2025 and month in (4, 5, 6):
            continue
        for region_index, (region_id, _, _) in enumerate(REGIONS):
            order_count = 3 + ((month + region_index) % 3)
            for local_index in range(order_count):
                order_id = f"O{order_number:04d}"
                order_day = 2 + ((local_index * 7 + region_index * 3) % 25)
                order_date = date(year, month, order_day)
                customer_region_id = (
                    REGIONS[(region_index + 1) % len(REGIONS)][0]
                    if order_number % 5 == 0
                    else region_id
                )
                region_customers = [
                    customer
                    for customer in CUSTOMERS
                    if customer[2] == customer_region_id
                ]
                customer = region_customers[(month + local_index) % len(region_customers)]
                is_return = (order_number % 11) == 0
                promotion = (
                    ["PROMO_A", "PROMO_B"][(month + region_index) % 2]
                    if (order_number % 4) == 0
                    else None
                )
                orders.append(
                    {
                        "order_id": order_id,
                        "order_date": order_date.isoformat(),
                        "customer_id": customer[0],
                        "region_id": region_id,
                        "status": "returned" if is_return else "completed",
                        "promotion_code": promotion,
                    }
                )

                line_count = 1 + (order_number % 3)
                for line_index in range(line_count):
                    if order_number % 17 == 0 and line_index == 0:
                        product_id = "P08"
                    else:
                        product_id = PRODUCTS[
                            (month * 3 + region_index * 2 + local_index + line_index)
                            % 7
                        ][0]
                    quantity = 1 + rng.randrange(3)
                    price, cost = product_prices[product_id]
                    discount = Decimal("0.10") if promotion and line_index == 0 else Decimal("0")
                    sign = Decimal("-1") if is_return else Decimal("1")
                    net_revenue = price * quantity * (Decimal("1") - discount) * sign
                    total_cost = cost * quantity * sign
                    items.append(
                        {
                            "order_item_id": f"I{item_number:05d}",
                            "order_id": order_id,
                            "product_id": product_id,
                            "quantity": quantity,
                            "unit_price": money(price),
                            "unit_cost": money(cost),
                            "discount_rate": money(discount),
                            "net_revenue": money(net_revenue),
                            "total_cost": money(total_cost),
                        }
                    )
                    item_number += 1
                order_number += 1

    write_csv(
        output / "dates.csv",
        ["date_id", "year", "quarter", "month", "day_of_week"],
        dates,
    )
    write_csv(
        output / "regions.csv",
        ["region_id", "region_name", "region_code"],
        regions,
    )
    write_csv(
        output / "products.csv",
        ["product_id", "product_name", "category", "unit_price", "unit_cost"],
        products,
    )
    write_csv(
        output / "customers.csv",
        ["customer_id", "customer_label", "home_region_id", "segment"],
        customers,
    )
    write_csv(
        output / "orders.csv",
        [
            "order_id",
            "order_date",
            "customer_id",
            "region_id",
            "status",
            "promotion_code",
        ],
        orders,
    )
    write_csv(
        output / "order_items.csv",
        [
            "order_item_id",
            "order_id",
            "product_id",
            "quantity",
            "unit_price",
            "unit_cost",
            "discount_rate",
            "net_revenue",
            "total_cost",
        ],
        items,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    generate(args.output)


if __name__ == "__main__":
    main()
