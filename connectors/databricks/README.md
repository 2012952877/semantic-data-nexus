# Azure Databricks resolver

This independently testable Python 3.12 package is the clean-room M0 boundary between a
validated physical SQL fragment and the Azure Databricks SQL Statement Execution API. It does
not accept natural language, compile SQL, persist credentials, or provision Azure resources.

## Contract

`DatabricksResolver` accepts a `PhysicalSourceFragment` containing one read-only `SELECT` or
`WITH` query and named, typed parameters. It returns a normalized `TabularResult` with column
metadata, string-or-null cells (the API's `JSON_ARRAY` contract), and non-secret lineage:

- resolver kind and statement identifier;
- a SHA-256 fingerprint of the physical fragment, never the SQL text;
- structured diagnostics containing stable codes, elapsed time, state, and safe request IDs.

The future optimizer can call `resolver.capabilities()` before producing the fragment. The
declaration covers SELECT, FILTER, AGGREGATE, JOIN, SORT, and LIMIT; common aggregate functions;
the API result types; named parameterization; asynchronous execution; cancellation; result
chunks; and the supported result disposition/format combinations.

## Install and validate

Run these commands from this directory:

```shell
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m mypy src
python -m build
```

All normal tests use `FakeTransport` and make no network calls.
The read-only boundary parses Databricks SQL with `sqlglot` and accepts exactly one query AST;
parse failures, command/DDL/DML nodes (including inside CTEs), positional parameters, and
functions outside the M0 allowlist fail closed. Function authorization uses the exact,
unqualified, unquoted source identifier rather than SQLGlot's canonical function name. The
allowlist contains the advertised aggregates plus `ABS`, `CAST`, `COALESCE`, `CURRENT_DATE`,
`DATE_ADD`, `DATE_SUB`, `DATEDIFF`, `DATE_TRUNC`, `DAY`, `LOWER`, `MONTH`, `ROUND`, `TO_DATE`,
`UPPER`, and `YEAR`. Internal parser aliases, unresolved calls, qualified calls, and UDFs are not
authorized in M0.

## Authentication

OAuth is preferred for automation. Implement `BearerTokenProvider.get_token()` with an
OAuth token source that refreshes credentials in memory, such as a workload or managed identity
integration. This package deliberately does not persist tokens or client secrets.

For local PAT use, set `DATABRICKS_TOKEN` in the process environment and construct
`EnvironmentPatProvider`. Do not place the value in source, command-line arguments, profiles
committed to version control, test fixtures, or logs. PATs are a legacy option and should be
short-lived and scoped to the required APIs.

The principal needs permission to use the selected SQL warehouse plus `USE CATALOG`,
`USE SCHEMA`, and `SELECT` on the data objects addressed by the validated fragment. Grant only
the minimum objects required.

Official references:

- [Statement Execution API tutorial](https://learn.microsoft.com/azure/databricks/dev-tools/sql-execution-tutorial)
- [Statement Execution API reference](https://docs.databricks.com/api/azure/workspace/statementexecution)
- [OAuth service-principal authorization](https://learn.microsoft.com/azure/databricks/dev-tools/auth/oauth-m2m)
- [Personal access token authentication](https://learn.microsoft.com/azure/databricks/dev-tools/auth/pat)

## Configuration

Use a workspace origin such as `https://workspace.example.invalid`; hosts containing paths,
queries, fragments, user information, or non-HTTPS schemes are rejected. Supply either a
warehouse ID or its SQL connector HTTP path. The HTTP path is an interoperability input only:
the Statement Execution API request sends `warehouse_id`.

Defaults use the synthetic `demo_sales` catalog and `analytics` schema. The package sends
`row_limit` and `byte_limit` to Databricks and fails closed when the manifest reports
truncation. Polling uses bounded exponential backoff and a monotonic deadline. Deadline
expiration can request cancellation before returning a timeout error.
Submit requests derive enough HTTP timeout headroom for the configured server-side wait.

## Local fake demo

```python
import asyncio

from semantic_data_nexus_databricks import (
    BearerToken,
    BearerTokenProvider,
    DatabricksResolver,
    PhysicalSourceFragment,
    ResolverConfig,
    StatementExecutionClient,
    StatementParameter,
)
from semantic_data_nexus_databricks.testing import FakeTransport

class DemoTokenProvider(BearerTokenProvider):
    async def get_token(self) -> BearerToken:
        return BearerToken("synthetic-demo-token")

transport = FakeTransport()
transport.enqueue(
    "POST",
    "https://workspace.example.invalid/api/2.0/sql/statements",
    json_body={
        "statement_id": "statement-demo",
        "status": {"state": "SUCCEEDED"},
        "manifest": {
            "format": "JSON_ARRAY",
            "schema": {
                "columns": [{
                    "name": "region",
                    "position": 0,
                    "type_name": "STRING",
                    "type_text": "STRING",
                }]
            },
            "truncated": False,
        },
        "result": {"data_array": [["north"]]},
    },
)
client = StatementExecutionClient(
    ResolverConfig(
        workspace_host="workspace.example.invalid",
        warehouse_id="warehouse-demo",
    ),
    DemoTokenProvider(),
    transport=transport,
)
fragment = PhysicalSourceFragment(
    source_name="demo_sales",
    sql="SELECT region FROM orders WHERE region = :region",
    parameters=(StatementParameter.string("region", "north"),),
)
result = asyncio.run(DatabricksResolver(client).resolve(fragment))
assert result.rows == (("north",),)
transport.assert_drained()
```

The fake validates method and URL ordering and never opens a network connection.

## Live smoke test

The optional smoke test is skipped unless all four variables below are present:

- `DATABRICKS_HOST`
- `DATABRICKS_TOKEN`
- `DATABRICKS_WAREHOUSE_ID`
- `DATABRICKS_SMOKE_STATEMENT`

The supplied statement must pass the same read-only validator. The smoke test enforces a
20-second deadline, 10-row limit, and 1 MiB byte limit. It never prints the statement, token,
host, parameters, rows, or response payload.

## Security and operations

- Parameter values are sent through API parameter markers and never logged.
- External links are short-lived credentials. The client never logs them and fetches them
  without the Databricks authorization header. API-supplied external headers are treated as
  sensitive, an external `Authorization` header is rejected, and HTTPX request logging strips
  URL query strings before records reach handlers.
- M0 supports `JSON_ARRAY` for both `INLINE` and `EXTERNAL_LINKS`; incompatible formats are
  rejected during configuration, before a statement can be submitted. External results can be
  chunked.
- Request IDs may be emitted for support correlation. SQL, rows, workspace configuration,
  authorization values, external URLs, and external headers are excluded from diagnostics.
- Decimal parameters use `DecimalType(precision, scale)` (or
  `StatementParameter.decimal(...)`) so fractional scale is never left to a server default.
- HTTP 429 and service errors are surfaced; callers should coordinate concurrency and
  rate-limit policy above this package. Polling backoff applies only to in-progress statements.
- Cancellation is a request, not proof of a terminal state. Callers that require confirmation
  should continue polling the statement identifier.
