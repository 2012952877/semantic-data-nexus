# Deterministic synthetic sales corpus

This clean-room corpus models a fictional sales business. Every name, identifier,
amount, and relationship is generated for this repository. It contains no personal
information or production-derived value.

Run the Python 3.12 generator from the repository root:

```sh
python data/synthetic/generate.py
```

The fixed seed is `20240917`. Output is written to `data/synthetic/generated/`.
Generation is byte-for-byte deterministic across Windows and Linux because CSV
files use UTF-8 and explicit newline handling.

## Schema

| Table | Grain | Important columns |
| --- | --- | --- |
| `dates` | calendar day | `date_id`, `year`, `quarter`, `month`, `day_of_week` |
| `regions` | sales region | `region_id`, `region_name`, `region_code` |
| `products` | product | `product_id`, `product_name`, `category`, unit price/cost |
| `customers` | fictional customer | `customer_id`, synthetic label, home region, segment |
| `orders` | order | date, customer, selling region, status, nullable promotion |
| `order_items` | order line | product, quantity, price/cost, discount, signed revenue/cost |

`net_revenue` and `total_cost` are negative on returned orders. Gross profit is
`net_revenue - total_cost`. The corpus intentionally includes:

- four regions and multiple product categories;
- nullable promotion codes and discounted lines;
- returned orders represented by signed amounts;
- two products with the duplicate-looking name `晨星标准版` but distinct IDs;
- free trial lines that create a zero revenue denominator;
- calendar dates for 2025-Q2 but no orders in that period.

The CSV values are source fixtures. Consumers should not infer a future
foundation storage or contract shape from these files.
