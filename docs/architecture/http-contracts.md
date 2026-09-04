# HTTP integration contracts

The Control API is the authenticated BFF. Browser clients call `/api/v1`; the
BFF calls the semantic backend at `/v1`. JSON property names are camel case,
enums use the literal values below, and every response is bounded and typed.

## Start a run

`POST /api/v1/runs` requires the `contributor` policy and accepts:

```json
{
  "clientRequestId": "request-001",
  "workload": "synthetic-workload",
  "question": "Compare synthetic regional revenue",
  "evaluationClock": "2026-08-15T09:00:00+08:00",
  "evaluationTimezone": "Asia/Shanghai",
  "compilationMode": "regional_quarterly_profit",
  "executionMode": "thread",
  "outputMode": "normal"
}
```

All eight fields are required, and unknown JSON members are rejected.
`clientRequestId` and `workload` contain 1-64 ASCII letters, digits, `.`, `_`,
or `-` and begin with a letter or digit. `question` is 1-4,000 Unicode scalar
values and rejects Unicode category C characters (control, format, surrogate,
private-use, and unassigned). `evaluationClock` must contain `Z` or an explicit
numeric UTC offset, and `evaluationTimezone` must be a valid IANA name,
including the `UTC` link. Compilation mode is `regional_quarterly_profit` or
`monthly_regional_comparison`; execution mode is `thread`; output mode is
`normal` or `stream`.

The BFF forwards exactly these fields to `POST /v1/runs`, plus `runId`,
`requestedBy`, and `traceId`. The stable `runId`, principal-scoped
`clientRequestId`, workload, question, and options are retained for
idempotency and safe start reconciliation.

## Read run detail

`GET /api/v1/runs/{runId}/detail` requires the `reader` policy and proxies
`GET /v1/runs/{runId}/detail`. The response has this typed shape:

```json
{
  "runId": "run_0123456789abcdef0123456789abcdef",
  "question": "Compare synthetic regional revenue",
  "sqg": {
    "version": "0.1",
    "intent": "Compare synthetic regional revenue",
    "ontology": "synthetic-sales",
    "resolvedMembers": ["region"],
    "metrics": ["net_sales"],
    "dimensions": ["region"],
    "filters": [{ "field": "period", "operator": "between", "value": "2025-Q1..2025-Q2" }],
    "policyChecks": ["governed"]
  },
  "physicalNodes": [{
    "id": "aggregate-region",
    "kind": "AGGREGATE",
    "label": "Aggregate by region",
    "plainLanguage": "Groups synthetic sales by region.",
    "inputs": ["synthetic-sales"],
    "outputFields": ["region", "net_sales"]
  }],
  "result": {
    "columns": [{
      "key": "region",
      "label": "Region",
      "dataType": "string",
      "format": "text",
      "nullable": false
    }],
    "rows": [["North"]],
    "rowCount": 1,
    "truncated": false
  },
  "manifest": {
    "resultId": "result-synthetic",
    "runId": "run_0123456789abcdef0123456789abcdef",
    "nodeId": "aggregate-region",
    "storage": "parquet",
    "uri": "results/run/result-synthetic",
    "rowCount": 1,
    "byteCount": 128,
    "checksum": "sha256:synthetic",
    "committedAt": "2026-01-01T00:00:00Z"
  },
  "lineage": {
    "version": "query-runtime/v0",
    "runId": "run_0123456789abcdef0123456789abcdef",
    "nodes": [],
    "edges": []
  },
  "diagnostics": []
}
```

Result data is column-oriented by schema: each row is an array whose cell at
index `n` must match column `n`. Cells may only be JSON null, string, integer,
finite number, or boolean values. Integers use the interoperable range
`[-9007199254740991, 9007199254740991]`; finite numbers have absolute value at
most `10^28`. Booleans are not integers. Dates are `yyyy-MM-dd` strings and
timestamps require an explicit UTC offset. Objects and arrays are rejected.

The BFF accepts at most 100 result columns, 1,000 inline rows, 1,000 physical
nodes, 5,000 lineage nodes, 10,000 lineage edges, and 1,000 diagnostics. It
also rejects unknown JSON properties, duplicate node or column IDs, invalid
references, out-of-order diagnostic sequences, mismatched nested run IDs, and
a question that differs from the persisted create request. Governed enum,
boolean, and count members are required rather than defaulted when absent.

## Errors and transport

The route group applies the existing authentication and per-principal/IP rate
limit. BFF errors are RFC 7807 Problem Details. Invalid local IDs return 400,
missing local runs return 404, semantic-backend timeouts return 504, and
transport failures, non-success responses, or invalid backend payloads return
502. The HTTP client buffers no more than 1 MiB and uses the configured
1-30 second timeout.
