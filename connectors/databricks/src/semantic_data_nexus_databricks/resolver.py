from __future__ import annotations

import hashlib

from sqlglot import exp, parse
from sqlglot.errors import SqlglotError

from .client import StatementExecutionClient
from .exceptions import ProtocolError, UnsafeStatementError
from .models import (
    CapabilityDeclaration,
    LineageMetadata,
    PhysicalSourceFragment,
    ResultFormat,
    TabularResult,
)

_SAFE_FUNCTIONS = frozenset(
    {
        "ABS",
        "AVG",
        "CAST",
        "COALESCE",
        "COUNT",
        "CURRENT_DATE",
        "DATEDIFF",
        "DAY",
        "LOWER",
        "MAX",
        "MIN",
        "MONTH",
        "ROUND",
        "SUM",
        "TIMESTAMP_TRUNC",
        "TS_OR_DS_ADD",
        "TS_OR_DS_TO_DATE",
        "UPPER",
        "YEAR",
    }
)


def validate_fragment(fragment: PhysicalSourceFragment) -> None:
    if not fragment.source_name.strip():
        raise UnsafeStatementError("source_name is required")
    if not fragment.sql.strip():
        raise UnsafeStatementError("SQL statement is empty")
    try:
        parsed = parse(fragment.sql, read="databricks")
    except SqlglotError as error:
        raise UnsafeStatementError("SQL is not valid Databricks syntax") from error
    statements = [
        statement
        for statement in parsed
        if statement is not None and not isinstance(statement, exp.Semicolon)
    ]
    if len(statements) != 1:
        raise UnsafeStatementError("Exactly one SQL statement is required")
    statement = statements[0]
    if statement is None or not isinstance(statement, exp.Query):
        raise UnsafeStatementError("M0 accepts only a read-only query")
    if any(
        isinstance(node, (exp.DML, exp.DDL, exp.Command))
        for node in statement.walk()
    ):
        raise UnsafeStatementError("Query AST contains a non-read-only operation")
    if statement.find(exp.Into) is not None:
        raise UnsafeStatementError("SELECT INTO is not read-only")
    if any(not isinstance(cte.this, exp.Query) for cte in statement.find_all(exp.CTE)):
        raise UnsafeStatementError("Every CTE body must be a read-only query")
    if statement.find(exp.Parameter) is not None:
        raise UnsafeStatementError("Only named parameter markers are supported")
    if any(
        function.sql_name() not in _SAFE_FUNCTIONS
        for function in statement.find_all(exp.Func)
    ):
        raise UnsafeStatementError("Query contains a function outside the M0 allowlist")

    placeholders = tuple(statement.find_all(exp.Placeholder))
    if any(placeholder.name == "?" or not placeholder.this for placeholder in placeholders):
        raise UnsafeStatementError("Positional parameters are not supported")
    markers = {placeholder.name for placeholder in placeholders}
    names = [parameter.name for parameter in fragment.parameters]
    if len(names) != len(set(names)):
        raise UnsafeStatementError("Parameter names must be unique")
    supplied = set(names)
    if markers != supplied:
        raise UnsafeStatementError("Named SQL markers and supplied parameters must match")


class DatabricksResolver:
    def __init__(self, client: StatementExecutionClient) -> None:
        self._client = client

    @staticmethod
    def capabilities() -> CapabilityDeclaration:
        return CapabilityDeclaration.databricks_sql()

    async def resolve(self, fragment: PhysicalSourceFragment) -> TabularResult:
        validate_fragment(fragment)
        result = await self._client.execute(fragment.sql, fragment.parameters)
        if result.result_format is not ResultFormat.JSON_ARRAY:
            raise ProtocolError("Normalized tabular resolution requires JSON_ARRAY format")
        return TabularResult(
            columns=result.columns,
            rows=result.rows,
            lineage=LineageMetadata(
                resolver="azure_databricks_statement_execution",
                source_name=fragment.source_name,
                statement_id=result.statement_id,
                physical_fragment_sha256=hashlib.sha256(fragment.sql.encode()).hexdigest(),
            ),
            elapsed_ms=result.elapsed_ms,
        )
