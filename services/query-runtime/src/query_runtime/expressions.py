"""Safe rendering of typed expressions produced by validated plans."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from query_runtime.domain import ExpressionKind, ScalarType, TypedExpression
from query_runtime.errors import OperatorFailure


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
    expression: TypedExpression, available_columns: set[str], *, exact_literals: bool = False
) -> RenderedExpression:
    if expression.kind is ExpressionKind.COLUMN:
        assert expression.column is not None
        return RenderedExpression(quote_identifier(expression.column, available_columns), ())
    if expression.kind is ExpressionKind.LITERAL:
        if exact_literals and expression.data_type is ScalarType.DECIMAL:
            if expression.value is None:
                return RenderedExpression("CAST(? AS DECIMAL(38, 0))", (None,))
            if not isinstance(expression.value, str):
                raise OperatorFailure("EXPRESSION_TYPE", "Exact decimal literals require strings")
            try:
                value = Decimal(expression.value)
            except InvalidOperation as exc:
                raise OperatorFailure("EXPRESSION_TYPE", "Invalid decimal literal") from exc
            if not value.is_finite():
                raise OperatorFailure("EXPRESSION_TYPE", "Decimal must be finite")
            scale = max(0, -int(value.as_tuple().exponent))
            precision = max(1, len(value.as_tuple().digits), value.adjusted() + scale + 1)
            if precision > 38 or scale > 38:
                raise OperatorFailure("EXPRESSION_TYPE", "Decimal exceeds precision 38")
            return RenderedExpression(f"CAST(? AS DECIMAL(38, {scale}))", (str(value),))
        return RenderedExpression(
            f"CAST(? AS {_SQL_TYPES[expression.data_type]})",
            (expression.value,),
        )
    rendered = tuple(
        render_expression(arg, available_columns, exact_literals=exact_literals)
        for arg in expression.args
    )
    parameters = tuple(param for item in rendered for param in item.parameters)
    if expression.kind in _BINARY:
        _require_arity(expression, 2)
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
        return RenderedExpression(
            f"COALESCE({', '.join(item.sql for item in rendered)})", parameters
        )
    raise OperatorFailure(
        "EXPRESSION_UNSUPPORTED", f"Unsupported expression kind '{expression.kind}'"
    )


def _require_arity(expression: TypedExpression, expected: int) -> None:
    if len(expression.args) != expected:
        raise OperatorFailure(
            "EXPRESSION_ARITY",
            f"Expression '{expression.kind}' requires {expected} arguments",
        )
