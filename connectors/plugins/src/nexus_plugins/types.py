from __future__ import annotations

from decimal import Decimal

import pyarrow as pa
from query_runtime.domain import ExpressionKind, ScalarType, TypedExpression
from query_runtime.errors import ResolverFailure
from query_runtime.scalar_values import decimal_shape, scalar_value

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
        try:
            native = scalar_value(kind.value, value)
        except ValueError as exc:
            raise ResolverFailure("SOURCE_TYPE_MISMATCH", str(exc)) from exc
        assert isinstance(native, Decimal) or native is None
        integer_digits, scale = decimal_shape(native) if native is not None else (0, 0)
        return pa.decimal128(max(1, integer_digits + scale), scale)
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
            try:
                scalar_value(node.data_type.value, node.value)
            except ValueError as exc:
                raise ResolverFailure("PREDICATE_TYPE", str(exc)) from exc
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
