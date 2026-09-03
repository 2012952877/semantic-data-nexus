from __future__ import annotations

import hashlib
import re

from .client import StatementExecutionClient
from .exceptions import ProtocolError, UnsafeStatementError
from .models import (
    CapabilityDeclaration,
    LineageMetadata,
    PhysicalSourceFragment,
    ResultFormat,
    TabularResult,
)

_DENIED_KEYWORDS = frozenset(
    {
        "ALTER",
        "CALL",
        "COPY",
        "CREATE",
        "DELETE",
        "DROP",
        "GRANT",
        "INSERT",
        "MERGE",
        "OPTIMIZE",
        "REPLACE",
        "RESTORE",
        "REVOKE",
        "SET",
        "TRUNCATE",
        "UPDATE",
        "USE",
        "VACUUM",
    }
)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MARKER = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


def _mask_literals_and_comments(sql: str) -> str:
    output: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(sql):
        current = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if quote is not None:
            output.append(" ")
            if current == quote:
                if following == quote:
                    output.append(" ")
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if current in ("'", '"', "`"):
            quote = current
            output.append(" ")
            index += 1
            continue
        if current == "-" and following == "-":
            while index < len(sql) and sql[index] not in "\r\n":
                output.append(" ")
                index += 1
            continue
        if current == "/" and following == "*":
            output.extend((" ", " "))
            index += 2
            while index < len(sql):
                if sql[index] == "*" and index + 1 < len(sql) and sql[index + 1] == "/":
                    output.extend((" ", " "))
                    index += 2
                    break
                output.append(" ")
                index += 1
            else:
                raise UnsafeStatementError("SQL contains an unterminated block comment")
            continue
        output.append(current)
        index += 1
    if quote is not None:
        raise UnsafeStatementError("SQL contains an unterminated quoted value")
    return "".join(output)


def validate_fragment(fragment: PhysicalSourceFragment) -> None:
    if not fragment.source_name.strip():
        raise UnsafeStatementError("source_name is required")
    masked = _mask_literals_and_comments(fragment.sql).strip()
    if not masked:
        raise UnsafeStatementError("SQL statement is empty")
    if ";" in masked:
        if masked.count(";") != 1 or not masked.endswith(";"):
            raise UnsafeStatementError("Multiple SQL statements are not allowed")
        masked = masked[:-1].rstrip()

    words = [match.group(0).upper() for match in _WORD.finditer(masked)]
    if not words or words[0] not in {"SELECT", "WITH"}:
        raise UnsafeStatementError("M0 accepts only SELECT or WITH queries")
    denied = _DENIED_KEYWORDS.intersection(words)
    if denied:
        raise UnsafeStatementError(
            f"Read-only statement contains denied keyword {sorted(denied)[0]}"
        )
    if "?" in masked:
        raise UnsafeStatementError("Positional parameters are not supported")

    markers = set(_MARKER.findall(masked))
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
