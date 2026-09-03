"""Local DuckDB execution over Arrow tables."""

from __future__ import annotations

import asyncio
import math
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import duckdb
import pyarrow as pa

from query_runtime.domain import AggregateFunction, OperatorKind, OperatorSpec
from query_runtime.errors import OperatorFailure, ResourceLimitFailure, RuntimeFailure
from query_runtime.expressions import quote_identifier, render_expression


@dataclass(frozen=True)
class ResourceLimits:
    max_rows: int = 1_000_000
    max_bytes: int = 256 * 1024 * 1024
    max_in_flight_bytes: int = 512 * 1024 * 1024
    memory_limit_bytes: int = 256 * 1024 * 1024
    node_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_rows,
            self.max_bytes,
            self.max_in_flight_bytes,
            self.memory_limit_bytes,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in integer_limits
        ):
            raise ValueError("row, byte, and memory limits must be integers")
        if (
            isinstance(self.node_timeout_seconds, bool)
            or not isinstance(self.node_timeout_seconds, (int, float))
            or not math.isfinite(self.node_timeout_seconds)
        ):
            raise ValueError("node timeout must be a finite number")
        if any(value <= 0 for value in integer_limits) or self.node_timeout_seconds <= 0:
            raise ValueError("resource limits must be positive")


class DuckDBOperatorExecutor:
    def __init__(self, limits: ResourceLimits | None = None) -> None:
        self.limits = limits or ResourceLimits()

    async def execute(
        self,
        spec: OperatorSpec,
        inputs: tuple[pa.Table, ...],
        cancel_event: asyncio.Event,
    ) -> pa.Table:
        if cancel_event.is_set():
            raise asyncio.CancelledError
        connection = duckdb.connect(
            ":memory:",
            config={"memory_limit": f"{self.limits.memory_limit_bytes}B"},
        )
        work = asyncio.create_task(
            asyncio.to_thread(self._execute_sync, connection, spec, inputs)
        )
        cancelled = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {work, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancelled in done and cancel_event.is_set() and not work.done():
                connection.interrupt()
                try:
                    await work
                except Exception:
                    pass
                raise asyncio.CancelledError
            result = await work
            self.enforce_limits(result)
            return result
        except asyncio.CancelledError:
            connection.interrupt()
            with suppress(Exception):
                await asyncio.shield(work)
            raise
        finally:
            cancelled.cancel()
            if not work.done():
                connection.interrupt()
                with suppress(Exception):
                    await asyncio.shield(work)
            connection.close()

    def _execute_sync(
        self,
        connection: duckdb.DuckDBPyConnection,
        spec: OperatorSpec,
        inputs: tuple[pa.Table, ...],
    ) -> pa.Table:
        required_inputs = 2 if spec.kind is OperatorKind.JOIN else 1
        if len(inputs) != required_inputs:
            raise OperatorFailure(
                "OPERATOR_INPUT_COUNT",
                f"{spec.kind} requires {required_inputs} input table(s)",
            )
        for index, table in enumerate(inputs):
            connection.register(f"input_{index}", table)
        try:
            sql, parameters = self._build_query(spec, inputs)
            return connection.execute(sql, parameters).to_arrow_table()
        except RuntimeFailure:
            raise
        except duckdb.Error as exc:
            raise OperatorFailure(
                "DUCKDB_EXECUTION_FAILED", "DuckDB could not execute the validated operator"
            ) from exc
        finally:
            for index in range(len(inputs)):
                connection.unregister(f"input_{index}")

    def _build_query(
        self, spec: OperatorSpec, inputs: tuple[pa.Table, ...]
    ) -> tuple[str, list[Any]]:
        columns = set(inputs[0].column_names)
        if spec.kind in {OperatorKind.SELECT, OperatorKind.PROJECT}:
            return self._select_query(spec, columns)
        if spec.kind is OperatorKind.FILTER:
            if spec.predicate is None:
                raise OperatorFailure("OPERATOR_INVALID", "FILTER requires a predicate")
            rendered = render_expression(spec.predicate.expression, columns)
            return f"SELECT * FROM input_0 WHERE {rendered.sql}", list(rendered.parameters)
        if spec.kind is OperatorKind.AGGREGATE:
            if spec.time_grain is not None:
                raise OperatorFailure(
                    "OPERATOR_TIME_GRAIN_UNSUPPORTED",
                    "Local time-grain aggregation requires an explicit bucket expression",
                )
            return self._aggregate_query(spec, columns)
        if spec.kind is OperatorKind.PIVOT:
            return self._pivot_query(spec, columns)
        if spec.kind is OperatorKind.DERIVE:
            return self._derive_query(spec, columns)
        if spec.kind is OperatorKind.SORT:
            if not spec.sort:
                raise OperatorFailure("OPERATOR_INVALID", "SORT requires sort keys")
            terms = []
            for item in spec.sort:
                column = quote_identifier(item.column, columns)
                direction = item.direction.value.upper()
                nulls = "NULLS FIRST" if item.nulls_first else "NULLS LAST"
                terms.append(f"{column} {direction} {nulls}")
            return f"SELECT * FROM input_0 ORDER BY {', '.join(terms)}", []
        if spec.kind is OperatorKind.LIMIT:
            if spec.limit is None:
                raise OperatorFailure("OPERATOR_INVALID", "LIMIT requires a limit")
            return "SELECT * FROM input_0 LIMIT ?", [spec.limit]
        if spec.kind is OperatorKind.JOIN:
            return self._join_query(spec, inputs)
        raise OperatorFailure(
            "OPERATOR_UNSUPPORTED", f"Local operator '{spec.kind}' is not supported"
        )

    def _select_query(
        self, spec: OperatorSpec, columns: set[str]
    ) -> tuple[str, list[Any]]:
        names: set[str] = set()
        projections: list[str] = []
        parameters: list[Any] = []
        for column in spec.columns:
            if column in names:
                raise OperatorFailure("COLUMN_COLLISION", f"Duplicate output column '{column}'")
            names.add(column)
            projections.append(quote_identifier(column, columns))
        for item in spec.expressions:
            if item.name in names:
                raise OperatorFailure(
                    "COLUMN_COLLISION", f"Duplicate output column '{item.name}'"
                )
            names.add(item.name)
            rendered = render_expression(item.expression, columns)
            projections.append(f"{rendered.sql} AS {quote_identifier(item.name)}")
            parameters.extend(rendered.parameters)
        if not projections:
            raise OperatorFailure("OPERATOR_INVALID", f"{spec.kind} requires projections")
        return f"SELECT {', '.join(projections)} FROM input_0", parameters

    def _aggregate_query(
        self, spec: OperatorSpec, columns: set[str]
    ) -> tuple[str, list[Any]]:
        projections = [quote_identifier(column, columns) for column in spec.group_by]
        parameters: list[Any] = []
        output_names = set(spec.group_by)
        for aggregate in spec.aggregates:
            if aggregate.name in output_names:
                raise OperatorFailure(
                    "COLUMN_COLLISION", f"Duplicate output column '{aggregate.name}'"
                )
            output_names.add(aggregate.name)
            if aggregate.function is AggregateFunction.COUNT and aggregate.expression is None:
                expression_sql = "*"
            elif aggregate.expression is None:
                raise OperatorFailure(
                    "OPERATOR_INVALID",
                    f"{aggregate.function} requires an expression",
                )
            else:
                rendered = render_expression(aggregate.expression, columns)
                expression_sql = rendered.sql
                parameters.extend(rendered.parameters)
            projections.append(
                f"{aggregate.function.value.upper()}({expression_sql}) "
                f"AS {quote_identifier(aggregate.name)}"
            )
        if not spec.aggregates:
            raise OperatorFailure("OPERATOR_INVALID", "AGGREGATE requires aggregates")
        group = (
            " GROUP BY "
            + ", ".join(quote_identifier(column, columns) for column in spec.group_by)
            if spec.group_by
            else ""
        )
        return f"SELECT {', '.join(projections)} FROM input_0{group}", parameters

    def _pivot_query(
        self, spec: OperatorSpec, columns: set[str]
    ) -> tuple[str, list[Any]]:
        if not spec.pivot_column or not spec.pivot_value or not spec.pivot_values:
            raise OperatorFailure(
                "OPERATOR_INVALID", "PIVOT requires column, value, and fixed values"
            )
        pivot_column = quote_identifier(spec.pivot_column, columns)
        pivot_value = quote_identifier(spec.pivot_value, columns)
        projections = [quote_identifier(column, columns) for column in spec.pivot_index]
        parameters: list[Any] = []
        output_names = set(spec.pivot_index)
        for value in spec.pivot_values:
            if value in output_names:
                raise OperatorFailure("COLUMN_COLLISION", f"Pivot output '{value}' collides")
            output_names.add(value)
            projections.append(
                f"SUM(CASE WHEN {pivot_column} = ? THEN {pivot_value} END) "
                f"AS {quote_identifier(value)}"
            )
            parameters.append(value)
        group = ", ".join(
            quote_identifier(column, columns) for column in spec.pivot_index
        )
        group_clause = f" GROUP BY {group}" if group else ""
        order_clause = f" ORDER BY {group}" if group else ""
        return (
            f"SELECT {', '.join(projections)} FROM input_0"
            f"{group_clause}{order_clause}",
            parameters,
        )

    def _derive_query(
        self, spec: OperatorSpec, columns: set[str]
    ) -> tuple[str, list[Any]]:
        projections = ["*"]
        parameters: list[Any] = []
        names = set(columns)
        for item in spec.expressions:
            if item.name in names:
                raise OperatorFailure(
                    "COLUMN_COLLISION", f"Derived column '{item.name}' already exists"
                )
            names.add(item.name)
            rendered = render_expression(item.expression, columns)
            projections.append(f"{rendered.sql} AS {quote_identifier(item.name)}")
            parameters.extend(rendered.parameters)
        if not spec.expressions:
            raise OperatorFailure("OPERATOR_INVALID", "DERIVE requires expressions")
        return f"SELECT {', '.join(projections)} FROM input_0", parameters

    def _join_query(
        self, spec: OperatorSpec, inputs: tuple[pa.Table, ...]
    ) -> tuple[str, list[Any]]:
        if not spec.join_type or not spec.join_keys:
            raise OperatorFailure("OPERATOR_INVALID", "JOIN requires type and keys")
        left_columns = set(inputs[0].column_names)
        right_columns = set(inputs[1].column_names)
        collisions = sorted(left_columns & right_columns)
        if collisions:
            raise OperatorFailure(
                "COLUMN_COLLISION",
                "JOIN inputs contain colliding column names",
                details={"columns": collisions},
            )
        clauses = []
        for key in spec.join_keys:
            left = quote_identifier(key.left, left_columns)
            right = quote_identifier(key.right, right_columns)
            clauses.append(f"left_input.{left} = right_input.{right}")
        join_type = spec.join_type.value.upper()
        sql = (
            "SELECT left_input.*, right_input.* FROM input_0 AS left_input "
            f"{join_type} JOIN input_1 AS right_input ON {' AND '.join(clauses)}"
        )
        return sql, []

    def enforce_limits(self, table: pa.Table) -> None:
        if table.num_rows > self.limits.max_rows:
            raise ResourceLimitFailure(
                "LIMIT_ROWS_EXCEEDED",
                "Operator output exceeded the configured row limit",
                details={"rows": table.num_rows, "limit": self.limits.max_rows},
            )
        if table.nbytes > self.limits.max_bytes:
            raise ResourceLimitFailure(
                "LIMIT_BYTES_EXCEEDED",
                "Operator output exceeded the configured byte limit",
                details={"bytes": table.nbytes, "limit": self.limits.max_bytes},
            )
