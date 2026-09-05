"""Safe rendering of typed expressions produced by validated plans."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pyarrow as pa

from query_runtime.domain import ExpressionKind, ScalarType, TypedExpression
from query_runtime.errors import OperatorFailure
from query_runtime.scalar_values import decimal_shape, scalar_value


@dataclass(frozen=True)
class RenderedExpression:
    sql: str
    parameters: tuple[Any, ...]


_BINARY = {
    ExpressionKind.ADD: "+",
    ExpressionKind.SUBTRACT: "-",
    ExpressionKind.MULTIPLY: "*",
    ExpressionKind.EQUAL: "=",
    ExpressionKind.NOT_EQUAL: "<>",
    ExpressionKind.LESS_THAN: "<",
    ExpressionKind.LESS_EQUAL: "<=",
    ExpressionKind.GREATER_THAN: ">",
    ExpressionKind.GREATER_EQUAL: ">=",
    ExpressionKind.AND: "AND",
    ExpressionKind.OR: "OR",
}

_SQL_TYPES = {
    ScalarType.STRING: "VARCHAR",
    ScalarType.INTEGER: "BIGINT",
    ScalarType.FLOAT: "DOUBLE",
    ScalarType.DECIMAL: "DECIMAL(38, 10)",
    ScalarType.BOOLEAN: "BOOLEAN",
    ScalarType.DATE: "DATE",
    ScalarType.TIMESTAMP: "TIMESTAMP",
}

_DECIMAL_ARITHMETIC = frozenset(
    {
        ExpressionKind.ADD,
        ExpressionKind.SUBTRACT,
        ExpressionKind.MULTIPLY,
    }
)


def quote_identifier(identifier: str, available: set[str] | None = None) -> str:
    if not identifier or "\x00" in identifier:
        raise OperatorFailure("EXPRESSION_INVALID_IDENTIFIER", "Invalid empty identifier")
    if available is not None and identifier not in available:
        raise OperatorFailure(
            "COLUMN_MISSING",
            f"Column '{identifier}' is not present in the input",
            details={"column": identifier},
        )
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def render_expression(
    expression: TypedExpression,
    available_columns: set[str],
    *,
    exact_literals: bool = False,
    column_types: Mapping[str, pa.DataType] | None = None,
) -> RenderedExpression:
    if expression.kind is ExpressionKind.COLUMN:
        assert expression.column is not None
        return RenderedExpression(quote_identifier(expression.column, available_columns), ())
    if expression.kind is ExpressionKind.LITERAL:
        if exact_literals:
            try:
                native = scalar_value(expression.data_type.value, expression.value)
            except ValueError as exc:
                raise OperatorFailure("EXPRESSION_TYPE", str(exc)) from exc
        if exact_literals and expression.data_type is ScalarType.DECIMAL:
            assert isinstance(native, Decimal) or native is None
            integer_digits, scale = decimal_shape(native) if native is not None else (0, 0)
            precision = max(1, integer_digits + scale)
            return RenderedExpression(
                f"CAST(? AS DECIMAL({precision}, {scale}))",
                (format(native, "f") if native is not None else None,),
            )
        return RenderedExpression(
            f"CAST(? AS {_SQL_TYPES[expression.data_type]})",
            (expression.value,),
        )
    rendered = tuple(
        render_expression(
            arg, available_columns, exact_literals=exact_literals, column_types=column_types
        )
        for arg in expression.args
    )
    parameters = tuple(param for item in rendered for param in item.parameters)
    if expression.kind in _BINARY:
        _require_arity(expression, 2)
        if (
            exact_literals
            and expression.kind in _DECIMAL_ARITHMETIC
            and expression.data_type is ScalarType.DECIMAL
            and _is_exact_numeric_expression(expression)
        ):
            types = column_types or {}
            integer_digits, scale = _decimal_expression_shape(expression, types)
            precision = max(1, integer_digits + scale)
            operand_scales = (
                [_decimal_expression_shape(arg, types)[1] for arg in expression.args]
                if expression.kind is ExpressionKind.MULTIPLY
                else [scale, scale]
            )
            # Promote before arithmetic: an outer cast cannot rescue DECIMAL(18) carry.
            widened_operands = [
                f"CAST({item.sql} AS DECIMAL({precision}, {operand_scale}))"
                for item, operand_scale in zip(rendered, operand_scales, strict=True)
            ]
            return RenderedExpression(
                f"CAST(({widened_operands[0]} {_BINARY[expression.kind]} {widened_operands[1]}) "
                f"AS DECIMAL({precision}, {scale}))",
                parameters,
            )
        return RenderedExpression(
            f"({rendered[0].sql} {_BINARY[expression.kind]} {rendered[1].sql})",
            parameters,
        )
    if expression.kind is ExpressionKind.DIVIDE:
        _require_arity(expression, 2)
        return RenderedExpression(
            f"({rendered[0].sql} / NULLIF({rendered[1].sql}, 0))",
            parameters,
        )
    if expression.kind is ExpressionKind.NOT:
        _require_arity(expression, 1)
        return RenderedExpression(f"(NOT {rendered[0].sql})", parameters)
    if expression.kind is ExpressionKind.IS_NULL:
        _require_arity(expression, 1)
        return RenderedExpression(f"({rendered[0].sql} IS NULL)", parameters)
    if expression.kind is ExpressionKind.COALESCE:
        if not rendered:
            raise OperatorFailure("EXPRESSION_ARITY", "coalesce requires at least one argument")
        if exact_literals and expression.data_type is ScalarType.DECIMAL:
            integer_digits, scale = _decimal_expression_shape(expression, column_types or {})
            precision = max(1, integer_digits + scale)
            # Cast all operands to one lossless type before DuckDB chooses a common type.
            operands = ", ".join(
                f"CAST({item.sql} AS DECIMAL({precision}, {scale}))" for item in rendered
            )
            return RenderedExpression(f"COALESCE({operands})", parameters)
        return RenderedExpression(
            f"COALESCE({', '.join(item.sql for item in rendered)})", parameters
        )
    raise OperatorFailure(
        "EXPRESSION_UNSUPPORTED", f"Unsupported expression kind '{expression.kind}'"
    )


def _is_exact_numeric_expression(expression: TypedExpression) -> bool:
    if expression.data_type not in {ScalarType.DECIMAL, ScalarType.INTEGER}:
        return False
    if expression.kind in {ExpressionKind.COLUMN, ExpressionKind.LITERAL}:
        return True
    return expression.kind in _DECIMAL_ARITHMETIC | {ExpressionKind.COALESCE} and all(
        _is_exact_numeric_expression(arg) for arg in expression.args
    )


def _decimal_expression_shape(
    expression: TypedExpression,
    column_types: Mapping[str, pa.DataType],
) -> tuple[int, int]:
    if expression.data_type is ScalarType.INTEGER:
        if expression.kind is ExpressionKind.LITERAL:
            value = scalar_value(expression.data_type.value, expression.value)
            assert type(value) is int or value is None
            return (len(str(abs(value))) if value else 0), 0
        if expression.kind is ExpressionKind.COLUMN:
            data_type = column_types.get(expression.column or "")
            if data_type is None or not pa.types.is_integer(data_type):
                raise OperatorFailure("EXPRESSION_TYPE", "Integer column requires its Arrow schema")
            sign_bits = 0 if pa.types.is_unsigned_integer(data_type) else 1
            return len(str(2 ** (data_type.bit_width - sign_bits))), 0
        return 19, 0
    if expression.data_type is not ScalarType.DECIMAL:
        raise OperatorFailure("EXPRESSION_TYPE", "Exact decimal operands are required")
    if expression.kind is ExpressionKind.LITERAL:
        native = scalar_value(expression.data_type.value, expression.value)
        assert isinstance(native, Decimal) or native is None
        return decimal_shape(native) if native is not None else (0, 0)
    if expression.kind is ExpressionKind.COLUMN:
        data_type = column_types.get(expression.column or "")
        if data_type is None or not pa.types.is_decimal128(data_type):
            raise OperatorFailure("EXPRESSION_TYPE", "Decimal column requires its Arrow schema")
        return int(data_type.precision - data_type.scale), int(data_type.scale)
    shapes = [_decimal_expression_shape(arg, column_types) for arg in expression.args]
    if expression.kind is ExpressionKind.COALESCE:
        integer_digits = max(digits for digits, _ in shapes)
        scale = max(scale for _, scale in shapes)
        if integer_digits + scale > 38:
            raise OperatorFailure(
                "EXPRESSION_TYPE",
                "Decimal COALESCE has no lossless common type within precision 38",
            )
        return integer_digits, scale
    if expression.kind in _DECIMAL_ARITHMETIC:
        if expression.kind is ExpressionKind.MULTIPLY:
            scale = sum(scale for _, scale in shapes)
            integer_digits = sum(digits for digits, _ in shapes)
        else:
            scale = max(scale for _, scale in shapes)
            integer_digits = max(digits for digits, _ in shapes) + 1
        if scale > 38:
            raise OperatorFailure("EXPRESSION_TYPE", "Decimal expression scale exceeds 38")
        # DuckDB caps arithmetic width at 38 without reducing scale; overflow remains an error.
        return min(integer_digits, 38 - scale), scale
    raise OperatorFailure("EXPRESSION_TYPE", "Expression does not produce an exact decimal")


def _require_arity(expression: TypedExpression, expected: int) -> None:
    if len(expression.args) != expected:
        raise OperatorFailure(
            "EXPRESSION_ARITY",
            f"Expression '{expression.kind}' requires {expected} arguments",
        )
