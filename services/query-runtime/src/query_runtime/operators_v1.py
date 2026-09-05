"""Finite relational extensions. Every SQL token comes from typed fields or identifiers."""

from __future__ import annotations

from datetime import date as date_value
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from query_runtime.domain import OperatorKind, OperatorSpecV1, ScalarType
from query_runtime.errors import OperatorFailure
from query_runtime.expressions import quote_identifier

if TYPE_CHECKING:
    from query_runtime.operators import DuckDBOperatorExecutor

SET_OPERATORS = frozenset(
    {
        OperatorKind.UNION_ALL,
        OperatorKind.UNION_DISTINCT,
        OperatorKind.INTERSECT,
        OperatorKind.EXCEPT,
    }
)


def _invalid(message: str) -> OperatorFailure:
    return OperatorFailure("OPERATOR_INVALID", message)


def _new_name(name: str, columns: set[str]) -> str:
    if name.lower() in {column.lower() for column in columns}:
        raise OperatorFailure("COLUMN_COLLISION", "Extension output collides with input")
    return quote_identifier(name)


def _order(spec: OperatorSpecV1, columns: set[str], *, tie_break: bool = True) -> str:
    terms = [
        f"{quote_identifier(item.column, columns)} {item.direction.value.upper()} "
        f"NULLS {'FIRST' if item.nulls_first else 'LAST'}"
        for item in spec.sort
    ]
    if tie_break:
        terms.append("to_json(input_0)")
    return ", ".join(terms)


def build_extension_query(
    executor: DuckDBOperatorExecutor, spec: OperatorSpecV1, inputs: tuple[pa.Table, ...]
) -> tuple[str, list[Any]] | None:
    columns = set(inputs[0].column_names)
    kind = spec.kind
    if kind in SET_OPERATORS:
        if not inputs[0].schema.equals(inputs[1].schema, check_metadata=False):
            raise _invalid("Set inputs require identical ordered Arrow schemas")
        operation = {
            OperatorKind.UNION_ALL: "UNION ALL",
            OperatorKind.UNION_DISTINCT: "UNION",
            OperatorKind.INTERSECT: "INTERSECT",
            OperatorKind.EXCEPT: "EXCEPT",
        }[kind]
        return f"SELECT * FROM input_0 {operation} SELECT * FROM input_1", []
    if kind is OperatorKind.DISTINCT:
        projection = ", ".join(quote_identifier(c, columns) for c in spec.columns) or "*"
        return f"SELECT DISTINCT {projection} FROM input_0", []
    if kind in {OperatorKind.DEDUPLICATE, OperatorKind.PICK, OperatorKind.WINDOW}:
        partition = ", ".join(quote_identifier(c, columns) for c in spec.partition_by)
        prefix = f"PARTITION BY {partition} " if partition else ""
        order = _order(spec, columns)
        if kind in {OperatorKind.DEDUPLICATE, OperatorKind.PICK}:
            count = 1 if kind is OperatorKind.DEDUPLICATE else (spec.limit or 1)
            if spec.limit == 0:
                count = 0
            return (
                f"SELECT * FROM input_0 QUALIFY row_number() OVER "
                f"({prefix}ORDER BY {order}) <= ? ORDER BY {order}",
                [count],
            )
        assert spec.window is not None
        window = spec.window
        output = _new_name(window.output, columns)
        if window.function in {"rank", "dense_rank"}:
            order = _order(spec, columns, tie_break=False)
        expression = quote_identifier(window.column, columns) if window.column is not None else ""
        frame = (
            f" ROWS BETWEEN {window.preceding} PRECEDING AND CURRENT ROW"
            if window.column is not None
            else ""
        )
        return (
            f"SELECT *, {window.function}({expression}) OVER "
            f"({prefix}ORDER BY {order}{frame}) AS {output} FROM input_0 "
            f"ORDER BY {order}, {output}",
            [],
        )
    if kind is OperatorKind.SAMPLE:
        return (
            "SELECT * FROM input_0 ORDER BY "
            "md5(CAST(? AS VARCHAR) || ':' || to_json(input_0)), to_json(input_0) LIMIT ?",
            [spec.sample_seed, spec.limit],
        )
    if kind in {OperatorKind.DATE, OperatorKind.RESAMPLE}:
        assert spec.date is not None
        date = spec.date
        column = quote_identifier(date.column, columns)
        output = _new_name(date.output, columns)
        data_type = inputs[0].schema.field(date.column).type
        if not (pa.types.is_date(data_type) or pa.types.is_timestamp(data_type)):
            raise _invalid("DATE/RESAMPLE requires an Arrow date or timestamp")
        if pa.types.is_timestamp(data_type) and data_type.tz is not None:
            raise _invalid("DATE/RESAMPLE requires timezone-naive input; normalize at ingestion")
        bucket = f"date_trunc(?, {column})"
        if pa.types.is_date(data_type):
            bucket = f"CAST({bucket} AS DATE)"
        inner = f"SELECT *, {bucket} AS {output} FROM input_0"
        if kind is OperatorKind.DATE:
            return inner, [date.grain.value]
        aggregate = spec.model_copy(
            update={
                "kind": OperatorKind.AGGREGATE,
                "group_by": (date.output, *spec.group_by),
                "time_grain": None,
            }
        )
        sql, parameters = executor._aggregate_query(
            aggregate, columns | {date.output}, source="bucketed"
        )
        return (
            f"WITH bucketed AS ({inner}) {sql}",
            [date.grain.value, *parameters],
        )
    if kind is OperatorKind.SUMMARIZE:
        return executor._aggregate_query(spec, columns)
    if kind is OperatorKind.EXPLODE:
        assert spec.explode is not None
        column = quote_identifier(spec.explode.column, columns)
        output = _new_name(spec.explode.output, columns)
        data_type = inputs[0].schema.field(spec.explode.column).type
        if not (pa.types.is_list(data_type) or pa.types.is_large_list(data_type)):
            raise _invalid("EXPLODE requires an Arrow list")
        return f"SELECT *, unnest({column}) AS {output} FROM input_0", []
    if kind is OperatorKind.UNPIVOT:
        assert spec.unpivot is not None
        unpivot = spec.unpivot
        if len(set(unpivot.columns)) != len(unpivot.columns):
            raise _invalid("UNPIVOT requires unique columns")
        name = _new_name(unpivot.name_column, columns)
        value = _new_name(unpivot.value_column, columns | {unpivot.name_column})
        for column in unpivot.columns:
            quote_identifier(column, columns)
        types = {inputs[0].schema.field(column).type for column in unpivot.columns}
        if len(types) != 1:
            raise _invalid("UNPIVOT columns must have identical Arrow types")
        retained = [c for c in inputs[0].column_names if c not in unpivot.columns]
        prefix = "".join(f"{quote_identifier(c)}, " for c in retained)
        queries = [
            f"SELECT {prefix}CAST(? AS VARCHAR) AS {name}, "
            f"{quote_identifier(c)} AS {value} FROM input_0"
            for c in unpivot.columns
        ]
        return " UNION ALL ".join(queries), list(unpivot.columns)
    if kind is OperatorKind.IMPUTE:
        assert spec.impute is not None
        column = quote_identifier(spec.impute.column, columns)
        target_type = inputs[0].schema.field(spec.impute.column).type
        literal = spec.impute.value
        matches = {
            ScalarType.STRING: pa.types.is_string,
            ScalarType.INTEGER: pa.types.is_integer,
            ScalarType.FLOAT: pa.types.is_floating,
            ScalarType.DECIMAL: pa.types.is_decimal,
            ScalarType.BOOLEAN: pa.types.is_boolean,
            ScalarType.DATE: pa.types.is_date,
            ScalarType.TIMESTAMP: pa.types.is_timestamp,
        }
        if not matches[literal.data_type](target_type):
            raise _invalid("IMPUTE literal must match the target type")
        executor.render(literal, columns, exact=True)
        replacement: Any = literal.value
        if literal.data_type is ScalarType.DECIMAL:
            replacement = Decimal(str(literal.value))
        elif literal.data_type is ScalarType.DATE:
            replacement = date_value.fromisoformat(str(literal.value))
        elif literal.data_type is ScalarType.TIMESTAMP:
            replacement = datetime.fromisoformat(str(literal.value))
        try:
            replacement = pa.scalar(replacement, type=target_type).as_py()
        except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError) as exc:
            raise _invalid("IMPUTE replacement cannot be represented without loss") from exc
        expressions = [
            f"COALESCE({column}, cast_to_type(?, {column})) AS {column}"
            if c == spec.impute.column
            else quote_identifier(c)
            for c in inputs[0].column_names
        ]
        return f"SELECT {', '.join(expressions)} FROM input_0", [replacement]
    return None
