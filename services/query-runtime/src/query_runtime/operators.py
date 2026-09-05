"""Local DuckDB execution over Arrow tables."""

from __future__ import annotations

import asyncio
import math
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import duckdb
import pyarrow as pa

from query_runtime.arrow_memory import retained_table_size
from query_runtime.domain import (
    BLOCKED_OPERATOR_KINDS,
    AggregateFunction,
    OperatorKind,
    OperatorSpec,
    OperatorSpecV1,
    TypedExpression,
)
from query_runtime.errors import OperatorFailure, ResourceLimitFailure, RuntimeFailure
from query_runtime.expressions import RenderedExpression, quote_identifier, render_expression
from query_runtime.operators_v1 import SET_OPERATORS, build_extension_query


def operator_input_count(kind: OperatorKind) -> int:
    return 2 if kind in SET_OPERATORS | {OperatorKind.JOIN} else 1


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
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_limits):
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
        for table in inputs:
            self.enforce_limits(table)
        if sum(retained_table_size(table) for table in inputs) > self.limits.max_in_flight_bytes:
            raise ResourceLimitFailure("LIMIT_IN_FLIGHT_BYTES_EXCEEDED", "Inputs exceed budget")
        connection = duckdb.connect(
            ":memory:",
            config={
                "memory_limit": f"{self.limits.memory_limit_bytes}B",
                "threads": "1",
                "temp_directory": "",
                "autoinstall_known_extensions": "false",
                "autoload_known_extensions": "false",
            },
        )
        connection.execute("SET enable_external_access = false")
        work = asyncio.create_task(asyncio.to_thread(self._execute_sync, connection, spec, inputs))
        cancelled = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {work, cancelled},
                timeout=self.limits.node_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                connection.interrupt()
                await _await_worker(work)
                raise ResourceLimitFailure("LIMIT_TIMEOUT_EXCEEDED", "Compute deadline exceeded")
            if cancelled in done and cancel_event.is_set() and not work.done():
                connection.interrupt()
                await _await_worker(work)
                raise asyncio.CancelledError
            result = await work
            self.enforce_limits(result)
            return result
        except asyncio.CancelledError:
            connection.interrupt()
            await _await_worker(work)
            raise
        finally:
            cancelled.cancel()
            if not work.done():
                connection.interrupt()
                await _await_worker(work)
            connection.close()

    def _execute_sync(
        self,
        connection: duckdb.DuckDBPyConnection,
        spec: OperatorSpec,
        inputs: tuple[pa.Table, ...],
    ) -> pa.Table:
        required_inputs = operator_input_count(spec.kind)
        if len(inputs) != required_inputs:
            raise OperatorFailure(
                "OPERATOR_INPUT_COUNT",
                f"{spec.kind} requires {required_inputs} input table(s)",
            )
        for index, table in enumerate(inputs):
            connection.register(f"input_{index}", table)
        try:
            sql, parameters = self._build_query(spec, inputs)
            reader = connection.execute(sql, parameters).to_arrow_reader(batch_size=2048)
            if isinstance(spec, OperatorSpecV1) and spec.kind is OperatorKind.IMPUTE:
                if not reader.schema.equals(inputs[0].schema, check_metadata=False):
                    raise OperatorFailure(
                        "OPERATOR_TYPE_MISMATCH", "IMPUTE cannot change input types"
                    )
            batches: list[pa.RecordBatch] = []
            rows = 0
            size = 0
            for batch in reader:
                rows += batch.num_rows
                size += retained_table_size(pa.Table.from_batches([batch]))
                if rows > self.limits.max_rows:
                    raise ResourceLimitFailure("LIMIT_ROWS_EXCEEDED", "Compute row limit exceeded")
                if size > self.limits.max_bytes:
                    raise ResourceLimitFailure(
                        "LIMIT_BYTES_EXCEEDED", "Compute byte limit exceeded"
                    )
                batches.append(batch)
            return pa.Table.from_batches(batches, schema=reader.schema)
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
        for table in inputs:
            _reject_normalized_collisions(
                table.column_names, "Input contains colliding column names"
            )
        columns = set(inputs[0].column_names)
        if spec.kind in BLOCKED_OPERATOR_KINDS:
            raise OperatorFailure("OPERATOR_UNSUPPORTED", "A governed backend is required")
        if isinstance(spec, OperatorSpecV1):
            extended = build_extension_query(self, spec, inputs)
            if extended is not None:
                return extended
        if spec.kind in {OperatorKind.SELECT, OperatorKind.PROJECT}:
            return self._select_query(spec, columns)
        if spec.kind is OperatorKind.FILTER:
            if spec.predicate is None:
                raise OperatorFailure("OPERATOR_INVALID", "FILTER requires a predicate")
            rendered = self.render(
                spec.predicate.expression, columns, exact=isinstance(spec, OperatorSpecV1)
            )
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

    def _select_query(self, spec: OperatorSpec, columns: set[str]) -> tuple[str, list[Any]]:
        names: set[str] = set()
        projections: list[str] = []
        parameters: list[Any] = []
        for column in spec.columns:
            _add_output_name(names, column)
            projections.append(quote_identifier(column, columns))
        for item in spec.expressions:
            _add_output_name(names, item.name)
            rendered = self.render(item.expression, columns, exact=isinstance(spec, OperatorSpecV1))
            projections.append(f"{rendered.sql} AS {quote_identifier(item.name)}")
            parameters.extend(rendered.parameters)
        if not projections:
            raise OperatorFailure("OPERATOR_INVALID", f"{spec.kind} requires projections")
        return f"SELECT {', '.join(projections)} FROM input_0", parameters

    def _aggregate_query(self, spec: OperatorSpec, columns: set[str]) -> tuple[str, list[Any]]:
        projections = [quote_identifier(column, columns) for column in spec.group_by]
        parameters: list[Any] = []
        output_names: set[str] = set()
        for column in spec.group_by:
            _add_output_name(output_names, column)
        for aggregate in spec.aggregates:
            _add_output_name(output_names, aggregate.name)
            if aggregate.function is AggregateFunction.COUNT and aggregate.expression is None:
                expression_sql = "*"
            elif aggregate.expression is None:
                raise OperatorFailure(
                    "OPERATOR_INVALID",
                    f"{aggregate.function} requires an expression",
                )
            else:
                rendered = self.render(
                    aggregate.expression, columns, exact=isinstance(spec, OperatorSpecV1)
                )
                expression_sql = rendered.sql
                parameters.extend(rendered.parameters)
            projections.append(
                f"{aggregate.function.value.upper()}({expression_sql}) "
                f"AS {quote_identifier(aggregate.name)}"
            )
        if not spec.aggregates:
            raise OperatorFailure("OPERATOR_INVALID", "AGGREGATE requires aggregates")
        group = (
            " GROUP BY " + ", ".join(quote_identifier(column, columns) for column in spec.group_by)
            if spec.group_by
            else ""
        )
        return f"SELECT {', '.join(projections)} FROM input_0{group}", parameters

    def _pivot_query(self, spec: OperatorSpec, columns: set[str]) -> tuple[str, list[Any]]:
        if not spec.pivot_column or not spec.pivot_value or not spec.pivot_values:
            raise OperatorFailure(
                "OPERATOR_INVALID", "PIVOT requires column, value, and fixed values"
            )
        pivot_column = quote_identifier(spec.pivot_column, columns)
        pivot_value = quote_identifier(spec.pivot_value, columns)
        projections = [quote_identifier(column, columns) for column in spec.pivot_index]
        parameters: list[Any] = []
        output_names: set[str] = set()
        for column in spec.pivot_index:
            _add_output_name(output_names, column)
        for value in spec.pivot_values:
            _add_output_name(output_names, value)
            projections.append(
                f"SUM(CASE WHEN {pivot_column} = ? THEN {pivot_value} END) "
                f"AS {quote_identifier(value)}"
            )
            parameters.append(value)
        group = ", ".join(quote_identifier(column, columns) for column in spec.pivot_index)
        group_clause = f" GROUP BY {group}" if group else ""
        order_clause = f" ORDER BY {group}" if group else ""
        return (
            f"SELECT {', '.join(projections)} FROM input_0{group_clause}{order_clause}",
            parameters,
        )

    def _derive_query(self, spec: OperatorSpec, columns: set[str]) -> tuple[str, list[Any]]:
        projections = ["*"]
        parameters: list[Any] = []
        names = {_normalize_identifier(column) for column in columns}
        for item in spec.expressions:
            normalized = _normalize_identifier(item.name)
            if normalized in names:
                raise OperatorFailure(
                    "COLUMN_COLLISION", f"Derived column '{item.name}' already exists"
                )
            names.add(normalized)
            rendered = self.render(item.expression, columns, exact=isinstance(spec, OperatorSpecV1))
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
        left_normalized = {_normalize_identifier(column): column for column in left_columns}
        right_normalized = {_normalize_identifier(column): column for column in right_columns}
        collisions = [
            (left_normalized[name], right_normalized[name])
            for name in sorted(left_normalized.keys() & right_normalized.keys())
        ]
        if collisions:
            raise OperatorFailure(
                "COLUMN_COLLISION",
                "JOIN inputs contain colliding column names",
                details={"columns": [name for pair in collisions for name in pair]},
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
        retained_bytes = retained_table_size(table)
        if retained_bytes > self.limits.max_bytes:
            raise ResourceLimitFailure(
                "LIMIT_BYTES_EXCEEDED",
                "Operator output exceeded the configured byte limit",
                details={"bytes": retained_bytes, "limit": self.limits.max_bytes},
            )

    @staticmethod
    def render(
        expression: TypedExpression, columns: set[str], *, exact: bool
    ) -> RenderedExpression:
        return render_expression(expression, columns, exact_literals=exact)


def _normalize_identifier(name: str) -> str:
    return name.translate(_ASCII_IDENTIFIER_FOLD)


def _reject_normalized_collisions(names: list[str], message: str) -> None:
    normalized: dict[str, str] = {}
    for name in names:
        key = _normalize_identifier(name)
        existing = normalized.get(key)
        if existing is not None:
            raise OperatorFailure(
                "COLUMN_COLLISION",
                message,
                details={"columns": [existing, name]},
            )
        normalized[key] = name


def _add_output_name(names: set[str], name: str) -> None:
    normalized = _normalize_identifier(name)
    if normalized in names:
        raise OperatorFailure("COLUMN_COLLISION", f"Duplicate output column '{name}'")
    names.add(normalized)


_ASCII_IDENTIFIER_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


async def _await_worker(work: asyncio.Task[pa.Table]) -> None:
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    with suppress(asyncio.CancelledError, Exception):
        work.result()
