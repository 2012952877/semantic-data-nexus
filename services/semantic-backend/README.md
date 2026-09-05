# Semantic backend

The semantic backend owns M0 run orchestration. By default it invokes the deterministic
`semantic-api` compiler, converts only a succeeded validated SQG through the
versioned source-mapping adapter, plans and executes it with `query-runtime`,
and publishes bounded result and lineage details.

Offline mode is the default and reads the repository's deterministic synthetic
CSV corpus. The service never accepts SQL and does not log request content,
credentials, signed URLs, connection strings, or provider exception text.

## Model-provider configuration

The default constructor uses `SemanticCompiler.from_environment()` through its `default()`
factory. The server-side `SEMANTIC_COMPILER_*` settings documented in
[semantic-api](../semantic-api/README.md#structured-model-provider-m1) select either explicit
local/dev fixtures (default) or real structured model calls. This requires no Control API DTO,
Web or source-resolver configuration change. Compiler and source-resolver selection are
independent; selecting a model does not enable Databricks.

Readiness includes local provider configuration validity without a model call. Invalid settings
leave the service not ready and runs fail closed; there is no static fallback. Run cancellation
and application lifespan shutdown close in-flight provider HTTP requests. The backend's
30-second monotonic whole-run deadline starts when a new run is accepted. Queue wait,
initialization, compilation, repair, planning, execution and result generation share that
deadline, even when per-call provider timeouts are configured higher. Runtime nodes receive
only the remaining budget. Expiry cancels in-flight provider work, waits for cooperative HTTP
cleanup and releases the backend slot; it reports `RUN_TIMEOUT`, not user cancellation.
No next operation or late result is admitted once the deadline is exhausted. Resolver cleanup
still completes before terminal reporting and may take additional time to confirm cancellation.
Model output reaches the runtime only after authoritative SQG validation.

Detailed model/phase/usage metadata is on the compiler response and internal integrated
artifact. The existing public BFF run contract still exposes only aggregate integer token usage;
no new model/deployment fields are claimed on that contract. Successful real runs use actual
validated provider counts. The existing zero-initialized counters on failed/cancelled runs are
not billing records; consult compiler call metadata for known versus unknown usage.

This is the two-mode M1 provider boundary, not generalized compilation (#33) or production
acceptance. No live model calls or hosted-demo changes were made for this implementation.

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
# Enterprise identity boundary

The HTTP service defaults to authenticated service mode. It requires
`SEMANTIC_NEXUS_SERVICE_PUBLIC_KEY` (the BFF's RSA public PEM) and
`SEMANTIC_NEXUS_IDENTITY_POSTGRES` (injected PostgreSQL connection reference).
Every non-health request needs a single-use, request-bound service assertion and
current membership in the shared identity store. Caller scope/identity headers
are not trusted. Result and lineage aliases use the same scoped run lookup.

For the original offline synthetic examples, explicitly set
`SEMANTIC_NEXUS_AUTH_MODE=legacy-development` and
`SEMANTIC_NEXUS_ENVIRONMENT=Development`; no other environment permits this mode.
The original local Compose and offline CI opt in, but production never falls back.
See [enterprise identity operations](../../docs/operations/enterprise-identity.md)
for the real Keycloak login recipe, scope/transport contract and future endpoint
boundaries. Process-local runtime durability remains a separate #34 responsibility.
