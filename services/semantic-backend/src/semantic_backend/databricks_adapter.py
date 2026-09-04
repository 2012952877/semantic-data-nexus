from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

import pyarrow as pa
from query_runtime.domain import (
    AggregateFunction,
    CapabilityCatalog,
    ExpressionKind,
    OperatorKind,
    OperatorSpec,
    ScalarType,
    SourceFragment,
    TypedExpression,
)
from query_runtime.errors import ResolverFailure
from query_runtime.resolver import ExecutionContext
from semantic_data_nexus_databricks import (
    DatabricksResolverError,
    ParameterType,
    PhysicalSourceFragment,
    StatementParameter,
    TabularResult,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DECIMAL = re.compile(r"^DECIMAL\((\d+),(\d+)\)$")

_BINARY_SQL = {
    ExpressionKind.ADD: "+",
    ExpressionKind.SUBTRACT: "-",
    ExpressionKind.MULTIPLY: "*",
    ExpressionKind.DIVIDE: "/",
    ExpressionKind.EQUAL: "=",
    ExpressionKind.NOT_EQUAL: "<>",
    ExpressionKind.LESS_THAN: "<",
    ExpressionKind.LESS_EQUAL: "<=",
    ExpressionKind.GREATER_THAN: ">",
    ExpressionKind.GREATER_EQUAL: ">=",
    ExpressionKind.AND: "AND",
    ExpressionKind.OR: "OR",
}

_PARAMETER_TYPES = {
    ScalarType.STRING: ParameterType.STRING,
    ScalarType.INTEGER: ParameterType.LONG,
    ScalarType.FLOAT: ParameterType.DOUBLE,
    ScalarType.DECIMAL: ParameterType.DOUBLE,
    ScalarType.BOOLEAN: ParameterType.BOOLEAN,
    ScalarType.DATE: ParameterType.DATE,
    ScalarType.TIMESTAMP: ParameterType.TIMESTAMP,
}


class ConnectorResolver(Protocol):
    async def resolve(self, fragment: PhysicalSourceFragment) -> TabularResult: ...


class DatabricksFragmentTranslator:
    def __init__(
        self,
        *,
        catalog: str,
        schema: str,
        row_limit: int = 1_000,
    ) -> None:
        self.catalog = self._identifier(catalog)
        self.schema = self._identifier(schema)
        if isinstance(row_limit, bool) or not 1 <= row_limit <= 10_000:
            raise ValueError("row_limit must be between 1 and 10000")
        self.row_limit = row_limit
        self._parameters: list[StatementParameter] = []

    def translate(self, fragment: SourceFragment) -> PhysicalSourceFragment:
        self._parameters = []
        object_name = self._identifier(fragment.source.object_name)
        query = (
            f"SELECT * FROM {self._quote(self.catalog)}.{self._quote(self.schema)}."
            f"{self._quote(object_name)}"
        )
        for operation in fragment.operations:
            query = self._operation(query, operation)
        query = f"SELECT * FROM ({query}) AS bounded_result LIMIT {self.row_limit}"
        return PhysicalSourceFragment(
            source_name=fragment.source.alias,
            sql=query,
            parameters=tuple(self._parameters),
        )

    def _operation(self, query: str, spec: OperatorSpec) -> str:
        if spec.kind is OperatorKind.SOURCE:
            return query
        if spec.kind in {OperatorKind.SELECT, OperatorKind.PROJECT}:
            projections = [self._quote(self._identifier(column)) for column in spec.columns]
            projections.extend(
                f"{self._expression(item.expression)} AS {self._quote(self._identifier(item.name))}"
                for item in spec.expressions
            )
            if not projections:
                raise ResolverFailure(
                    "DATABRICKS_TRANSLATION_INVALID",
                    "A projection requires reviewed output columns.",
                )
            return f"SELECT {', '.join(projections)} FROM ({query}) AS projected"
        if spec.kind is OperatorKind.FILTER:
            if spec.predicate is None:
                raise ResolverFailure(
                    "DATABRICKS_TRANSLATION_INVALID",
                    "A filter requires a typed predicate.",
                )
            return (
                f"SELECT * FROM ({query}) AS filtered "
                f"WHERE {self._expression(spec.predicate.expression)}"
            )
        if spec.kind is OperatorKind.AGGREGATE:
            if not spec.aggregates:
                raise ResolverFailure(
                    "DATABRICKS_TRANSLATION_INVALID",
                    "An aggregate requires reviewed measures.",
                )
            group_by = [self._quote(self._identifier(column)) for column in spec.group_by]
            projections = list(group_by)
            for aggregate in spec.aggregates:
                function = AggregateFunction(aggregate.function).value.upper()
                if aggregate.expression is None:
                    if aggregate.function is not AggregateFunction.COUNT:
                        raise ResolverFailure(
                            "DATABRICKS_TRANSLATION_INVALID",
                            "Only COUNT may omit an aggregate expression.",
                        )
                    expression = "*"
                else:
                    expression = self._expression(aggregate.expression)
                projections.append(
                    f"{function}({expression}) AS {self._quote(self._identifier(aggregate.name))}"
                )
            group_clause = f" GROUP BY {', '.join(group_by)}" if group_by else ""
            return f"SELECT {', '.join(projections)} FROM ({query}) AS aggregated{group_clause}"
        if spec.kind is OperatorKind.SORT:
            if not spec.sort:
                raise ResolverFailure(
                    "DATABRICKS_TRANSLATION_INVALID",
                    "A sort requires reviewed keys.",
                )
            terms = [
                (
                    f"{self._quote(self._identifier(item.column))} "
                    f"{item.direction.value.upper()} "
                    f"{'NULLS FIRST' if item.nulls_first else 'NULLS LAST'}"
                )
                for item in spec.sort
            ]
            return f"SELECT * FROM ({query}) AS sorted ORDER BY {', '.join(terms)}"
        if spec.kind is OperatorKind.LIMIT:
            if spec.limit is None:
                raise ResolverFailure(
                    "DATABRICKS_TRANSLATION_INVALID",
                    "A limit requires a bounded row count.",
                )
            return f"SELECT * FROM ({query}) AS limited LIMIT {min(spec.limit, self.row_limit)}"
        raise ResolverFailure(
            "DATABRICKS_TRANSLATION_UNSUPPORTED",
            f"Operator '{spec.kind.value}' cannot cross the live source boundary.",
        )

    def _expression(self, expression: TypedExpression) -> str:
        if expression.kind is ExpressionKind.COLUMN:
            assert expression.column is not None
            return self._quote(self._identifier(expression.column))
        if expression.kind is ExpressionKind.LITERAL:
            parameter = self._parameter(expression.data_type, expression.value)
            return f":{parameter.name}"
        if expression.kind in _BINARY_SQL and len(expression.args) == 2:
            left, right = expression.args
            left_sql = self._expression(left)
            right_sql = self._expression(right)
            if expression.kind in {ExpressionKind.AND, ExpressionKind.OR}:
                left_sql = self._ungroup(left_sql)
                right_sql = self._ungroup(right_sql)
            return f"({left_sql} {_BINARY_SQL[expression.kind]} {right_sql})"
        if expression.kind is ExpressionKind.NOT and len(expression.args) == 1:
            return f"(NOT {self._expression(expression.args[0])})"
        if expression.kind is ExpressionKind.IS_NULL and len(expression.args) == 1:
            return f"({self._expression(expression.args[0])} IS NULL)"
        if expression.kind is ExpressionKind.COALESCE and expression.args:
            return f"COALESCE({', '.join(self._expression(item) for item in expression.args)})"
        raise ResolverFailure(
            "DATABRICKS_EXPRESSION_UNSUPPORTED",
            f"Expression '{expression.kind.value}' is outside the live allowlist.",
        )

    def _parameter(
        self,
        data_type: ScalarType,
        value: str | int | float | bool | None,
    ) -> StatementParameter:
        name = f"p{len(self._parameters)}"
        parameter = StatementParameter(
            name=name,
            type=_PARAMETER_TYPES[data_type],
            value=value,
        )
        self._parameters.append(parameter)
        return parameter

    @staticmethod
    def _identifier(value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ResolverFailure(
                "DATABRICKS_IDENTIFIER_INVALID",
                "A live source identifier is outside the reviewed ASCII allowlist.",
            )
        return value

    @staticmethod
    def _quote(value: str) -> str:
        return f"`{value}`"

    @staticmethod
    def _ungroup(value: str) -> str:
        return value[1:-1] if value.startswith("(") and value.endswith(")") else value


class DatabricksSourceAdapter:
    source_type = "azure_databricks_statement_execution"

    def __init__(
        self,
        resolver: ConnectorResolver,
        translator: DatabricksFragmentTranslator,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._resolver = resolver
        self._translator = translator
        self._timeout_seconds = timeout_seconds
        self._active_cancellations: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def execute(
        self,
        context: ExecutionContext,
        fragment: SourceFragment,
        cancel_event: asyncio.Event,
    ) -> pa.Table:
        connector_fragment = self._translator.translate(fragment)
        local_cancel = asyncio.Event()
        async with self._lock:
            if context.cancellation_handle in self._active_cancellations:
                raise ResolverFailure(
                    "DATABRICKS_HANDLE_CONFLICT",
                    "The opaque cancellation handle is already active.",
                )
            self._active_cancellations[context.cancellation_handle] = local_cancel

        resolution = asyncio.create_task(self._resolver.resolve(connector_fragment))
        cancellation = asyncio.create_task(self._wait_for_cancellation(cancel_event, local_cancel))
        try:
            done, _ = await asyncio.wait(
                {resolution, cancellation},
                timeout=self._timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if resolution in done:
                cancellation.cancel()
                result = resolution.result()
                return self._to_arrow(result)
            if cancellation in done:
                # The connector owns provider cancellation on its bounded timeout.
                # Drain it so no remote statement or HTTP task is orphaned.
                try:
                    await asyncio.wait_for(
                        asyncio.shield(resolution),
                        timeout=self._timeout_seconds,
                    )
                except (TimeoutError, DatabricksResolverError):
                    pass
                raise asyncio.CancelledError
            resolution.cancel()
            await asyncio.gather(resolution, return_exceptions=True)
            raise ResolverFailure(
                "DATABRICKS_ADAPTER_TIMEOUT",
                "The live resolver exceeded the bounded adapter timeout.",
            )
        finally:
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)
            async with self._lock:
                self._active_cancellations.pop(context.cancellation_handle, None)

    async def cancel(self, cancellation_handle: str) -> None:
        async with self._lock:
            event = self._active_cancellations.get(cancellation_handle)
            if event is not None:
                event.set()

    async def health(self) -> bool:
        return True

    async def capabilities(self, source_alias: str) -> CapabilityCatalog:
        return CapabilityCatalog(
            source_alias=source_alias,
            source_type="azure_databricks_statement_execution",
            operator_kinds=frozenset(
                {
                    OperatorKind.SELECT,
                    OperatorKind.FILTER,
                    OperatorKind.AGGREGATE,
                    OperatorKind.SORT,
                    OperatorKind.LIMIT,
                }
            ),
            aggregate_functions=frozenset(AggregateFunction),
            max_rows=self._translator.row_limit,
            max_bytes=10 * 1024 * 1024,
        )

    @staticmethod
    async def _wait_for_cancellation(
        runtime: asyncio.Event,
        adapter: asyncio.Event,
    ) -> None:
        runtime_wait = asyncio.create_task(runtime.wait())
        adapter_wait = asyncio.create_task(adapter.wait())
        try:
            await asyncio.wait(
                {runtime_wait, adapter_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            runtime_wait.cancel()
            adapter_wait.cancel()
            await asyncio.gather(runtime_wait, adapter_wait, return_exceptions=True)

    @staticmethod
    def _to_arrow(result: TabularResult) -> pa.Table:
        columns = sorted(result.columns, key=lambda item: item.position)
        if [item.position for item in columns] != list(range(len(columns))):
            raise ResolverFailure(
                "DATABRICKS_RESULT_SCHEMA_INVALID",
                "The live result column positions are invalid.",
            )
        names = [item.name for item in columns]
        if len(names) != len(set(names)):
            raise ResolverFailure(
                "DATABRICKS_RESULT_SCHEMA_INVALID",
                "The live result contains duplicate column names.",
            )
        if any(len(row) != len(columns) for row in result.rows):
            raise ResolverFailure(
                "DATABRICKS_RESULT_ROW_INVALID",
                "The live result row width does not match its schema.",
            )
        arrays = []
        for index, column in enumerate(columns):
            converter, arrow_type = _converter(column.type_name, column.type_text)
            try:
                arrays.append(
                    pa.array(
                        [converter(row[index]) for row in result.rows],
                        type=arrow_type,
                    )
                )
            except (ValueError, TypeError, pa.ArrowException) as exc:
                raise ResolverFailure(
                    "DATABRICKS_RESULT_VALUE_INVALID",
                    "The live result contained a value outside its declared type.",
                ) from exc
        return pa.Table.from_arrays(arrays, names=names)


def _converter(
    type_name: str,
    type_text: str,
) -> tuple[Callable[[str | None], object], pa.DataType]:
    normalized = type_name.upper()
    if normalized in {"STRING", "CHAR"}:
        return lambda value: value, pa.string()
    if normalized in {"BYTE", "SHORT", "INT", "LONG"}:
        return lambda value: None if value is None else int(value), pa.int64()
    if normalized in {"FLOAT", "DOUBLE"}:
        return lambda value: None if value is None else float(value), pa.float64()
    if normalized == "DECIMAL":
        match = _DECIMAL.fullmatch(type_text.upper())
        if match is None:
            raise ResolverFailure(
                "DATABRICKS_RESULT_SCHEMA_INVALID",
                "The live decimal type declaration is invalid.",
            )
        precision, scale = (int(value) for value in match.groups())
        return (
            lambda value: None if value is None else Decimal(value),
            pa.decimal128(precision, scale),
        )
    if normalized == "BOOLEAN":
        return _boolean, pa.bool_()
    if normalized == "DATE":
        return (
            lambda value: None if value is None else date.fromisoformat(value),
            pa.date32(),
        )
    if normalized == "TIMESTAMP":
        return (
            lambda value: (
                None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))
            ),
            pa.timestamp("us", tz="UTC"),
        )
    raise ResolverFailure(
        "DATABRICKS_RESULT_TYPE_UNSUPPORTED",
        f"Live result type '{normalized}' is outside the M0 allowlist.",
    )


def _boolean(value: str | None) -> bool | None:
    if value is None:
        return None
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    raise ValueError("invalid boolean")
