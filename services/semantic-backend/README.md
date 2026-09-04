# Semantic backend

The semantic backend owns M0 run orchestration. It invokes the deterministic
`semantic-api` compiler, converts only a succeeded validated SQG through the
versioned source-mapping adapter, plans and executes it with `query-runtime`,
and publishes bounded result and lineage details.

Offline mode is the default and reads the repository's deterministic synthetic
CSV corpus. The service never accepts SQL and does not log request content,
credentials, signed URLs, connection strings, or provider exception text.

## Run locally

From the repository root with Python 3.12:

```sh
python -m pip install \
  -e connectors/databricks \
  -e evals \
  -e services/semantic-api \
  -e services/query-runtime \
  -e "services/semantic-backend[databricks,dev,eval]"
semantic-backend
```

The service listens on port `8080` and exposes:

- `GET /health/live`
- `GET /health/ready`
- `POST /v1/runs`
- `GET /v1/runs/{run_id}`
- `POST /v1/runs/{run_id}/cancel`
- `GET /v1/runs/{run_id}/detail`

The start request uses the exact BFF contract:

```json
{
  "runId": "run_0123456789abcdef0123456789abcdef",
  "clientRequestId": "request-typed",
  "workload": "synthetic-workload",
  "question": "Compare synthetic regional revenue",
  "evaluationClock": "2026-08-15T09:00:00+08:00",
  "evaluationTimezone": "Asia/Shanghai",
  "compilationMode": "monthly_regional_comparison",
  "executionMode": "thread",
  "outputMode": "stream",
  "requestedBy": "synthetic-user",
  "traceId": "trace"
}
```

Unknown properties, missing required fields, noncanonical run IDs, invalid IANA
time zones, naive clocks, unsafe workload metadata, and questions over 4,000
characters are rejected. `contract-fixtures/v1/` contains the cross-language
start and detail fixtures. Detail rows are scalar arrays governed by their
typed columns and are bounded to 1,000 inline rows.

Run either verified scenario without starting an HTTP server:

```sh
python -m semantic_backend.demo --scenario simple
python -m semantic_backend.demo --scenario complex
```

Both use the fixed `2024-04-15T09:00:00Z` evaluation clock and checked-in
synthetic CSVs.

## Resolver boundary

`SEMANTIC_NEXUS_RESOLVER=fake` is the default and the only CI/demo execution
mode. `databricks` is optional and fails startup when required configuration is
missing. `.env.example` documents names only; never commit a populated `.env`.

The live adapter translates a runtime `SourceFragment` AST into one
parameterized, read-only statement. Identifiers must match the reviewed ASCII
allowlist, literal values become named connector parameters, local-only
operators never cross the boundary, and a final 1,000-row limit is mandatory.
The merged connector validates the resulting Databricks SQL AST again.

Required live settings are `DATABRICKS_WORKSPACE_HOST`,
`DATABRICKS_WAREHOUSE_ID`, and a deployment-secret `DATABRICKS_TOKEN`.
`DATABRICKS_CATALOG` and `DATABRICKS_SCHEMA` default to synthetic identifiers.
Live execution is bounded to 1,000 rows, 10 MiB, and a 30-second connector
timeout. The adapter requests provider cancellation immediately after a
statement ID is available and waits for the connector's terminal
acknowledgement/cleanup. A run is not reported `Cancelled` before that point;
unconfirmed provider cancellation is surfaced as a safe failure instead. Live
queries fetch one sentinel row and fail closed when more than 1,000 complete
rows would otherwise be silently omitted.

## Container

Build from the repository root:

```sh
docker build -f services/semantic-backend/Dockerfile -t semantic-backend:m0 .
docker run --rm -p 8080:8080 semantic-backend:m0
```

The image runs as a non-root user and defaults to the fake resolver. Run state
and inline results are intentionally process-local and non-durable in M0. The
root `.dockerignore` allowlists only package manifests, source trees, README
files, and generated synthetic CSVs so local `.env` files never enter a build
layer.

Run validation with:

```sh
python -m pytest services/semantic-backend
python -m ruff check services/semantic-backend
python -m mypy services/semantic-backend/src
python -m build services/semantic-backend
```

The integrated evaluator gates only two supported cases—regional quarterly
profit and monthly `AGGREGATE -> PIVOT -> DERIVE -> PROJECT`—at 100 in every
dimension. Candidates are emitted from the actual physical plan, committed
result metadata, runtime/connector lineage, and diagnostics. Mutation tests
prove plan, result, governance, and observability regressions are detected. It
does not claim coverage of the full reference suite.
