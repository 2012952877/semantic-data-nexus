from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pyarrow as pa


def default_data_directory() -> Path:
    configured = os.environ.get("SEMANTIC_NEXUS_DATA_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[4] / "data" / "synthetic" / "generated"


def load_synthetic_sales(data_directory: Path | None = None) -> pa.Table:
    data = (data_directory or default_data_directory()).resolve()
    required = ("orders.csv", "order_items.csv", "regions.csv")
    missing = [name for name in required if not (data / name).is_file()]
    if missing:
        raise FileNotFoundError("synthetic source files are missing: " + ", ".join(missing))

    connection = duckdb.connect()
    try:
        for table in ("orders", "order_items", "regions"):
            connection.read_csv(str(data / f"{table}.csv"), header=True).create_view(table)
        return connection.execute(
            """
            SELECT
                r.region_name AS region,
                CAST(date_trunc('quarter', CAST(o.order_date AS DATE)) AS TIMESTAMP)
                    AS period_quarter,
                CAST(date_trunc('month', CAST(o.order_date AS DATE)) AS TIMESTAMP)
                    AS period_month,
                CAST(i.net_revenue - i.total_cost AS DOUBLE) AS profit
            FROM orders AS o
            INNER JOIN order_items AS i USING (order_id)
            INNER JOIN regions AS r USING (region_id)
            """
        ).to_arrow_table()
    finally:
        connection.close()
