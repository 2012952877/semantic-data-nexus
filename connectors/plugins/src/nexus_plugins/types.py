from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal

import pyarrow as pa
from query_runtime.domain import ExpressionKind, ScalarType, TypedExpression
from query_runtime.errors import ResolverFailure

PREDICATE_KINDS = frozenset(
    {
        ExpressionKind.COLUMN,
        ExpressionKind.LITERAL,
        ExpressionKind.EQUAL,
        ExpressionKind.NOT_EQUAL,
        ExpressionKind.LESS_THAN,
        ExpressionKind.LESS_EQUAL,
        ExpressionKind.GREATER_THAN,
        ExpressionKind.GREATER_EQUAL,
        ExpressionKind.AND,
        ExpressionKind.OR,
        ExpressionKind.NOT,
        ExpressionKind.IS_NULL,
    }
)


def predicate_supported(expression: TypedExpression) -> bool:
    pending = [(expression, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > 16 or count > 128:
            raise ResolverFailure("PREDICATE_LIMIT", "Predicate exceeds depth/node budget")
        if node.kind not in PREDICATE_KINDS:
            return False
        pending.extend((arg, depth + 1) for arg in node.args)
    return True


def scalar_arrow_type(
    kind: ScalarType,
    value: str | int | float | bool | None = None,
) -> pa.DataType:
    if kind is ScalarType.DECIMAL:
        if value is not None and not isinstance(value, str):
            raise ResolverFailure("SOURCE_TYPE_MISMATCH", "Decimal literals require strings")
        decimal = Decimal(value or "0")
        if not decimal.is_finite():
            raise ResolverFailure("SOURCE_TYPE_MISMATCH", "Decimal must be finite")
        scale = max(0, -int(decimal.as_tuple().exponent))
        if max(scale, decimal.adjusted() + scale + 1) > 38:
            raise ResolverFailure("SOURCE_TYPE_MISMATCH", "Decimal exceeds precision 38")
        return pa.decimal128(38, scale)
    return {
        ScalarType.STRING: pa.string(),
        ScalarType.INTEGER: pa.int64(),
        ScalarType.FLOAT: pa.float64(),
        ScalarType.BOOLEAN: pa.bool_(),
        ScalarType.DATE: pa.date32(),
        ScalarType.TIMESTAMP: pa.timestamp("us"),
    }[kind]


def validate_predicate(expression: TypedExpression, schema: pa.Schema) -> None:
    pending = [(expression, 0)]
    count = 0
    for_current = {
        ScalarType.STRING: pa.types.is_string,
        ScalarType.INTEGER: pa.types.is_integer,
        ScalarType.FLOAT: pa.types.is_floating,
        ScalarType.DECIMAL: pa.types.is_decimal,
        ScalarType.BOOLEAN: pa.types.is_boolean,
        ScalarType.DATE: pa.types.is_date,
        ScalarType.TIMESTAMP: pa.types.is_timestamp,
    }
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > 16 or count > 128:
            raise ResolverFailure("PREDICATE_LIMIT", "Predicate exceeds depth/node budget")
        if node.kind is ExpressionKind.COLUMN:
            if node.column not in schema.names or not for_current[node.data_type](
                schema.field(node.column).type
            ):
                raise ResolverFailure("PREDICATE_TYPE", "Column does not match its declared type")
        elif node.kind is ExpressionKind.LITERAL:
            value = node.value
            if value is not None:
                valid = {
                    ScalarType.STRING: isinstance(value, str),
                    ScalarType.INTEGER: type(value) is int and -(2**63) <= value < 2**63,
                    ScalarType.FLOAT: type(value) in {int, float},
                    ScalarType.BOOLEAN: type(value) is bool,
                    ScalarType.DECIMAL: isinstance(value, str),
                    ScalarType.DATE: isinstance(value, str),
                    ScalarType.TIMESTAMP: isinstance(value, str),
                }[node.data_type]
                if not valid:
                    raise ResolverFailure(
                        "PREDICATE_TYPE", "Literal does not match its declared type"
                    )
                if node.data_type is ScalarType.DECIMAL:
                    scalar_arrow_type(node.data_type, value)
                if node.data_type is ScalarType.DATE:
                    date.fromisoformat(str(value))
                if node.data_type is ScalarType.TIMESTAMP:
                    if datetime.fromisoformat(str(value)).tzinfo is not None:
                        raise ResolverFailure("PREDICATE_TYPE", "Timestamp literals must be naive")
                if node.data_type is ScalarType.FLOAT and not math.isfinite(float(value)):
                    raise ResolverFailure("PREDICATE_TYPE", "Float must be finite")
        else:
            unary = node.kind in {ExpressionKind.NOT, ExpressionKind.IS_NULL}
            if len(node.args) != (1 if unary else 2) or node.data_type is not ScalarType.BOOLEAN:
                raise ResolverFailure("PREDICATE_TYPE", "Invalid predicate arity/result type")
            if node.kind in {ExpressionKind.AND, ExpressionKind.OR, ExpressionKind.NOT}:
                if any(arg.data_type is not ScalarType.BOOLEAN for arg in node.args):
                    raise ResolverFailure("PREDICATE_TYPE", "Boolean operands required")
            elif not unary and node.args[0].data_type is not node.args[1].data_type:
                raise ResolverFailure(
                    "PREDICATE_TYPE", "Comparison operands must have matching types"
                )
        pending.extend((arg, depth + 1) for arg in node.args)
