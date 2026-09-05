"""Compile a small, typed source-fragment language, not caller-provided SQL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import pyarrow as pa
from query_runtime.domain import OperatorKind, OperatorSpec, SourceFragment
from query_runtime.errors import ResolverFailure
from query_runtime.expressions import quote_identifier, render_expression
from query_runtime.operators import ResourceLimits
from sqlglot import Dialect, TokenType, exp, parse_one

from nexus_plugins.contracts import Asset
from nexus_plugins.types import predicate_supported, validate_predicate

PUSHDOWN = frozenset(
    {
        OperatorKind.SOURCE,
        OperatorKind.SELECT,
        OperatorKind.FILTER,
        OperatorKind.SORT,
        OperatorKind.LIMIT,
    }
)


@dataclass(frozen=True)
class CompiledRead:
    sql: str
    values: tuple[Any, ...]
    schema: pa.Schema


def validate_operation(operation: OperatorSpec) -> None:
    allowed = {
        OperatorKind.SOURCE: {"kind"},
        OperatorKind.SELECT: {"kind", "columns"},
        OperatorKind.FILTER: {"kind", "predicate"},
        OperatorKind.SORT: {"kind", "sort"},
        OperatorKind.LIMIT: {"kind", "limit"},
    }.get(operation.kind)
    if allowed is None or set(operation.model_dump(exclude_defaults=True)) - allowed:
        raise ResolverFailure("PUSHDOWN_UNSUPPORTED", "Unsupported source operation or parameter")
    if operation.kind is OperatorKind.SELECT and not operation.columns:
        raise ResolverFailure("PUSHDOWN_INVALID", "Projection cannot be empty")
    if operation.kind is OperatorKind.FILTER and operation.predicate is None:
        raise ResolverFailure("PUSHDOWN_INVALID", "Filter requires a predicate")
    if operation.kind is OperatorKind.SORT and not operation.sort:
        raise ResolverFailure("PUSHDOWN_INVALID", "Sort requires keys")
    if operation.kind is OperatorKind.LIMIT and operation.limit is None:
        raise ResolverFailure("PUSHDOWN_INVALID", "Limit requires a count")


def validate_fragment_shape(fragment: SourceFragment) -> None:
    if fragment.parameters:
        raise ResolverFailure("PUSHDOWN_INVALID", "Only typed expression literals bind parameters")
    if not fragment.operations or fragment.operations[0].kind is not OperatorKind.SOURCE:
        raise ResolverFailure("PUSHDOWN_INVALID", "Fragment must start with SOURCE")
    if len(fragment.operations) > 32:
        raise ResolverFailure("PUSHDOWN_INVALID", "Fragment exceeds 32 operations")
    for index, operation in enumerate(fragment.operations):
        validate_operation(operation)
        if index and operation.kind is OperatorKind.SOURCE:
            raise ResolverFailure("PUSHDOWN_INVALID", "SOURCE may appear only once")


def compile_read(
    fragment: SourceFragment,
    asset: Asset,
    dialect: Literal["postgres", "databricks"],
    limits: ResourceLimits,
) -> CompiledRead:
    validate_fragment_shape(fragment)
    expected_parts = 2 if dialect == "postgres" else 3
    if len(asset.table) != expected_parts:
        raise ResolverFailure("ASSET_INVALID", "Table must be fully qualified")
    schema = asset.schema
    projection = ", ".join(quote_identifier(c) for c in schema.names)
    table = ".".join(quote_identifier(c) for c in asset.table)
    sql = f"SELECT {projection} FROM {table}"
    parameters: list[Any] = []
    for operation in fragment.operations[1:]:
        columns = set(schema.names)
        if operation.kind is OperatorKind.SELECT:
            if len(set(operation.columns)) != len(operation.columns):
                raise ResolverFailure("PUSHDOWN_INVALID", "Projection must be unique")
            projection = ", ".join(quote_identifier(c, columns) for c in operation.columns)
            schema = pa.schema([schema.field(c) for c in operation.columns])
            sql = f"SELECT {projection} FROM ({sql}) AS selected"
        elif operation.kind is OperatorKind.FILTER:
            assert operation.predicate is not None
            expression = operation.predicate.expression
            if not predicate_supported(expression):
                raise ResolverFailure("PUSHDOWN_UNSUPPORTED", "Filter requires local compute")
            validate_predicate(expression, schema)
            rendered = render_expression(expression, columns, exact_literals=True)
            sql = f"SELECT * FROM ({sql}) AS filtered WHERE {rendered.sql}"
            parameters.extend(rendered.parameters)
        elif operation.kind is OperatorKind.SORT:
            terms = [
                f"{quote_identifier(s.column, columns)} {s.direction.value.upper()} "
                f"NULLS {'FIRST' if s.nulls_first else 'LAST'}"
                for s in operation.sort
            ]
            sql = f"SELECT * FROM ({sql}) AS ordered ORDER BY {', '.join(terms)}"
        elif operation.kind is OperatorKind.LIMIT:
            sql = f"SELECT * FROM ({sql}) AS limited LIMIT ?"
            parameters.append(operation.limit)
    sql = f"SELECT * FROM ({sql}) AS bounded_read LIMIT ?"
    parameters.append(limits.max_rows + 1)
    markers = [
        token
        for token in Dialect.get_or_raise("duckdb").tokenize(sql)
        if token.token_type is TokenType.PLACEHOLDER
    ]
    if len(markers) != len(parameters):
        raise ResolverFailure("PUSHDOWN_INVALID", "Generated parameter count mismatch")
    # AST traversal is not SQL lexical order (an outer LIMIT may be visited first).
    pieces: list[str] = []
    start = 0
    for index, marker in enumerate(markers):
        pieces.extend((sql[start : marker.start], f":p{index}"))
        start = marker.end + 1
    pieces.append(sql[start:])
    tree = parse_one("".join(pieces), read="duckdb")
    if dialect == "postgres":
        # Psycopg's parameter protocol requires literal percent signs to be doubled.
        for identifier in tree.find_all(exp.Identifier):
            identifier.set("this", identifier.name.replace("%", "%%"))
    generated = tree.sql(dialect=dialect)
    return CompiledRead(generated, tuple(parameters), schema)


def arrow_value(value: Any, data_type: pa.DataType) -> Any:
    if value is None:
        return None
    if pa.types.is_decimal(data_type):
        return Decimal(str(value))
    if pa.types.is_date(data_type) and isinstance(value, str):
        return date.fromisoformat(value)
    if pa.types.is_timestamp(data_type) and isinstance(value, str):
        return datetime.fromisoformat(value)
    if pa.types.is_boolean(data_type) and isinstance(value, str):
        if value not in {"true", "false"}:
            raise ResolverFailure("SOURCE_TYPE_MISMATCH", "Invalid boolean source value")
        return value == "true"
    if pa.types.is_integer(data_type) and isinstance(value, str):
        return int(value)
    if pa.types.is_floating(data_type) and isinstance(value, str):
        return float(value)
    return value


def rows_to_arrow(rows: list[tuple[Any, ...]], schema: pa.Schema) -> pa.Table:
    if any(len(row) != len(schema) for row in rows):
        raise ResolverFailure("SOURCE_SCHEMA_MISMATCH", "Source row width changed")
    arrays = [
        pa.array([arrow_value(row[i], field.type) for row in rows], type=field.type, safe=True)
        for i, field in enumerate(schema)
    ]
    table = pa.Table.from_arrays(arrays, schema=schema)
    if any(not field.nullable and table.column(i).null_count for i, field in enumerate(schema)):
        raise ResolverFailure("SOURCE_SCHEMA_MISMATCH", "Non-nullable source column contains null")
    return table
