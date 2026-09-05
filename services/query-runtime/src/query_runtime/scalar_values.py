"""Strict v1 scalar values, shared by models, source predicates and compute."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

type ScalarValue = str | int | float | bool | None
type NativeScalar = str | int | float | bool | Decimal | date | datetime | None


def decimal_shape(value: Decimal) -> tuple[int, int]:
    """Required integer digits and scale, without reserving unused integer digits."""
    if not value.is_finite():
        raise ValueError("Decimal must be finite")
    scale = max(0, -int(value.as_tuple().exponent))
    integer_digits = 0 if value.is_zero() else max(0, value.adjusted() + 1)
    if integer_digits + scale > 38:
        raise ValueError("Decimal exceeds precision 38")
    return integer_digits, scale


def scalar_value(kind: str, value: ScalarValue) -> NativeScalar:
    if value is None:
        return None
    if kind == "string" and isinstance(value, str):
        return value
    if kind == "integer" and type(value) is int and -(2**63) <= value < 2**63:
        return value
    if kind == "float" and type(value) in {int, float}:
        try:
            finite = math.isfinite(float(value))
        except OverflowError as exc:
            raise ValueError("Float value must be finite") from exc
        if finite:
            return value
    if kind == "boolean" and type(value) is bool:
        return value
    if kind == "decimal":
        if not isinstance(value, str):
            raise ValueError("Exact decimal literals require strings")
        try:
            decimal = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("Invalid decimal literal") from exc
        decimal_shape(decimal)
        if decimal.is_zero() and int(decimal.as_tuple().exponent) > 0:
            return Decimal(0)
        return decimal
    if kind == "date" and isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Invalid date literal") from exc
    if kind == "timestamp" and isinstance(value, str):
        fraction = re.search(r"[.,](\d+)", value)
        if fraction is not None and any(digit != "0" for digit in fraction[1][6:]):
            raise ValueError("Timestamp literal exceeds microsecond precision")
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Invalid timestamp literal") from exc
        if timestamp.tzinfo is not None:
            raise ValueError("Timestamp literals must be timezone-naive")
        return timestamp
    raise ValueError("Literal value does not match its declared scalar type")
