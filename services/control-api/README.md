# Control API

Typed ASP.NET Core 8 control-plane/BFF boundary for identity, run metadata,
cancellation, structured feedback, statistics, and the semantic backend.
It deliberately does not contain LLM calls, semantic compilation, SQL generation,
query execution, credentials, or result blobs.

## Run locally

The Development profile explicitly enables the local authentication handler and
the in-process fake semantic backend:

```powershell
dotnet run --project src\ControlApi\ControlApi.csproj
```

Supply these headers to authenticate a local request:

```text
X-Dev-Subject: local-developer
X-Dev-Name: Local Developer
X-Dev-Roles: reader,contributor,admin
```

Local header authentication is accepted only when the host environment is
exactly `Development`; enabling it in Staging, QA, Test, Production, or any
custom environment fails startup. The fake backend also fails in `Production`.
Production requires a configured Entra authority, tenant, client ID, and an
HTTPS semantic-backend base URI. JWT inbound claim mapping is disabled so Entra
`scp` and `roles` claims retain their protocol names. No client secret is
accepted or modeled. Future service-to-service credentials should use managed
identity.

## API contract

All business endpoints are under `/api/v1`.

| Method | Route | Policy | Purpose |
| --- | --- | --- | --- |
| GET | `/me` | reader | Current principal projection |
| POST | `/runs` | contributor | Idempotently create run metadata and start backend work |
| GET | `/runs?limit=50` | reader | List latest run projections |
| GET | `/runs/{runId}` | reader | Read run metadata and latest stored status |
| GET | `/runs/{runId}/detail` | reader | Read validated SQG, plan, result, manifest, lineage, and diagnostics |
| GET | `/runs/{runId}/semantic-status` | reader | Refresh status through the typed backend client |
| POST | `/runs/{runId}/cancel` | contributor | Idempotently request cancellation |
| POST | `/runs/{runId}/feedback` | contributor | Submit structured, version-checked feedback |
| GET | `/runs/{runId}/feedback` | reader | Read structured feedback |
| GET | `/statistics/summary` | admin | Aggregate the replaceable run store |
| GET | `/health/live` | anonymous | Process liveness |
| GET | `/health/ready` | anonymous | Semantic-backend readiness |

Roles or delegated scopes named `reader`, `contributor`, and `admin` satisfy the
matching policies. Contributor and admin permissions are cumulative.

`RunId` is serialized as `run_` followed by 32 lowercase hexadecimal characters.
Enums are serialized as strings. Errors use RFC 7807 Problem Details with a
stable `code`, distributed `traceId`, and `correlationId`. Callers may supply a
safe `X-Correlation-ID` containing up to 64 letters, digits, `.`, `_`, or `-`.

Create idempotency is scoped to principal plus `clientRequestId`. A run begins
in `StartPending`; timeout, transport loss, invalid success payload, or caller
cancellation moves it to retryable `DispatchUnknown`, never terminal `Failed`.
A duplicate create re-reads state after acquiring the per-`RunId` dispatch
lease. Only `StartPending` dispatches directly; `DispatchUnknown` first
reconciles by `RunId`, then repeats the idempotent backend start with the same
`RunId` only when the backend definitively reports it missing. Only a
definitive backend rejection marks the run failed.

Create requests carry the complete semantic execution input:

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

All eight members are required and unknown members are rejected. Idempotency
metadata is limited to 64 ASCII identifier characters and must begin with a
letter or digit. Questions are limited to 4,000 Unicode scalar values and may
not contain Unicode control, format, surrogate, private-use, or unassigned
characters. `evaluationClock` requires an explicit UTC offset,
`evaluationTimezone` must be exact `UTC` or a canonical slash-separated IANA
identifier. Validation is lexical and therefore independent of host timezone
data; single-component aliases such as `CET`, `GMT`, and `Japan` are rejected.
`compilationMode` is `regional_quarterly_profit` or
`monthly_regional_comparison`, `executionMode` is `thread`, and `outputMode`
is `normal` or `stream`. All fields participate in idempotency conflict
detection and are forwarded unchanged with the stable run ID, principal, and
trace ID.

In memory mode, start, reconciliation, status refresh, cancellation, and feedback mutation hold
a per-`RunId` dispatch lease in M0 so metadata writes cannot make a completed
backend start look pending and cancellation cannot lose to a late start.
Cancellation from `DispatchUnknown` first reconciles status by `RunId`: a
present backend run receives cancellation, while a definitive 404 finalizes
locally. A cancellation 404 triggers one status recheck so a run that never
reached the backend cannot remain permanently `CancelRequested`; ambiguous
failures stay retryable. A cancellation that acquires the lease before initial
dispatch finalizes locally without creating backend work. Cancellation persists
a monotonic generation
plus `Pending`/`Delivered` ownership. Failed delivery remains retryable, while a
delivered generation is not posted again. Active backend observations update
projection details without replacing local `CancelRequested`; a terminal
observation resolves it and also makes a racing delivery acknowledgment
idempotent. Feedback uses `submissionId` for idempotency and
`expectedRunVersion` for optimistic concurrency. Feedback is structured to
avoid an unrestricted text/prompt field. `submissionId`, `rating`, `outcome`,
`reasonCodes`, and `expectedRunVersion` are all required; `outcome` must be
`Helpful`, `PartiallyHelpful`, or `NotHelpful`.

## Configuration

`config\production.env.example` lists the environment variable boundary with no
credential values. Prefer deployment-time configuration and managed platform
identity. Do not place tokens, connection strings, or credentials in files.

- `AzureAd`: Microsoft.Identity.Web JWT bearer settings.
- `SemanticBackend`: base URI and a bounded 1-30 second timeout. Create calls
  have no transport-level retry. Control-plane reconciliation uses the stable
  `RunId` before an explicit repeat dispatch. Responses are rejected before
  persistence when IDs, states, timestamps, usage, diagnostics, stages, or
  nodes violate the bounded contract. Detail responses reject unknown JSON
  properties, mismatched run IDs or questions, object/array result cells, more
  than 100 columns or 1,000 inline rows, and oversized SQG, physical-plan,
  lineage, manifest, or diagnostic collections. Integer cells are limited to
  the interoperable range -9,007,199,254,740,991 through
  9,007,199,254,740,991. Float cells are finite JSON numbers with absolute
  values at or below 10^28; integral float values remain within the safe
  integer range so every accepted scalar round-trips without changing JSON
  token kind. Decimal cells are canonical fixed-point JSON
  strings with absolute values at or below 10^28, at most 29 significant
  digits, and scale 28 so precision and trailing scale survive the Python/.NET
  boundary.
- `ForwardedHeaders:KnownProxies`: explicit single-hop proxy IP allowlist.
  Unknown forwarders are ignored; header symmetry is required.
- `OpenTelemetry:Otlp:Endpoint`: optional OTLP traces, metrics, and logs.
- `ApplicationInsights:ConnectionString`: optional Azure Monitor OpenTelemetry
  distribution boundary, supplied only at deployment time.
- `OpenApi:Enabled`: OpenAPI is on in Development and off otherwise by default.
- `RateLimiting`: fixed-window per-principal/IP limits with explicit 429 Problem
  Details.

TLS terminates at the trusted ingress. Forwarded headers run before HSTS and
HTTPS redirection, and only loopback plus explicitly configured proxies are
trusted.

## Persistence boundary

`IRunRepository` has two adapters sharing pure `RunTransitions`. The default
`RunStorage__Provider=Memory` retains the process-local M0 behavior. Explicitly
set `RunStorage__Provider=Postgres` and inject `RunStorage__ConnectionString`
through an environment secret reference to enable Npgsql persistence. Unknown
providers, absent/invalid connection strings, inaccessible databases, and
migration failures prevent startup; there is **no fallback to memory**.
Postgres readiness is included in `/health/ready`; `/health/live` remains local.
No database or cloud resource is provisioned by the application.

The configured database/search path is the installation boundary. Use a dedicated
database and a migration-capable role for startup, backed up under your operations
policy; restrict database access to trusted application/operators. Require TLS
certificate verification in remote connection configuration. Connection pooling
uses separate repository and orchestration pools of at most 32 connections each,
with 5-second connection and 30-second command timeouts. The separate pools keep
lock waiters from starving repository work already inside a dispatch lease.
Parameter logging, server error detail and persisted security info are disabled.
Never put connection strings in source, command history, PRs, or log messages.

| Durable object | Concurrency and scope |
| --- | --- |
| Run metadata | JSONB preserves creation payload, stages/nodes, diagnostics, timestamps, usage, cancellation delivery/generation and version; row lock plus version-checked update |
| Create identity | Database unique `(subject, client_request_id)`; payload comparison happens after conflict serialization |
| Feedback | Unique `(run_id, submission_id)` under the current v1 contract, not subject-scoped; insertion and run version increment commit together |
| Start intent | Unique run ID and permanent generation 1, committed with `DispatchUnknown` before calling the backend |
| Statistics | Repeatable-read snapshot of validated runs and feedback, never a second volatile counter |

Public role authorization and DTOs remain unchanged. Subject-scoped create
identity is **not workspace isolation**: trusted workspace membership and
workspace-qualified keys belong to #32, guided by #29. A later additive migration
can introduce workspace keys and backfill existing data under an explicit mapping;
there is no fictitious tenant column or membership inference here.

### Dispatch ambiguity and recovery

Database advisory transaction locks serialize endpoint orchestration across
replicas, but are not a backend fence: a lost database connection may release a
lock while an old worker is still sending. The independent permanent start intent
prevents a second replica from sending another start even after lock loss.
The first claim commits `DispatchUnknown` with `durable_start_dispatch_claimed`;
it deliberately does not expire. Cancellation generation is independent.

If a process dies after claiming but before sending, the same creation payload
and subject/client request ID return the same run. Retries query the backend by
that known run ID. If found, validated status is reconciled; if absent, the API
returns `503 dispatch_recovery_required` without starting again. A cancellation
404 also cannot finalize a claimed run: an absent backend run does not fence a
paused original sender. The pending cancellation remains visible and can be
retried when the backend is available. Terminal observations absorb duplicate
terminal updates and late cancellation acknowledgments without replacing usage.

For `dispatch_recovery_required`, operators should:

1. Read the stored run, its diagnostic code and run ID; retain the original creation identity.
2. Establish whether the original sender is stopped/fenced and inspect backend status/logs for that run ID.
3. If the backend knows the run, repeat the original create or status-refresh request to reconcile; retry pending cancellation if applicable.
4. If the backend lost the run or acceptance cannot be established, retain the intent and escalate recovery to #34. Do not delete the claim, change the client request ID to bypass it, or blindly resend a start.

There is no automatic takeover, outbox worker, exactly-once backend execution,
event/result durability, or full-run crash recovery in this package. The
fail-closed case may require manual resolution until backend durable idempotency
and fencing are delivered in #34. It is not reported as successful recovery.

### Migrations, compatibility and retained data

Numbered embedded SQL files in `src/ControlApi/Persistence/Migrations` are applied
inside one transaction under a database advisory migration lock. Version and
SHA-256 checksum (normalized LF text) are recorded in `control_schema_versions`.
Concurrent first startups serialize, repeated applications do nothing, modified
checksums and newer schemas fail startup. Never edit a released migration;
append the next numbered file. Migration SQL and the version record roll back
together on failure; application startup never drops data.

This first schema has no down migration. Back up before upgrades. Rollback is
supported only to a binary that understands the same schema/version; do not
switch to memory as a rollback (that hides retained data). Future incompatible
changes require staged additive migrations and an explicit upgrade/restore plan.

Retention is indefinite; nothing silently expires or deletes runs, claims, or
feedback. Operators must budget/monitor database capacity and define a separately
reviewed archival policy. Lists remain limited to 100 runs. Metadata is bounded
by the existing typed stage/node/diagnostic validator and a 16 MiB database row
payload limit; each feedback payload has a 16 KiB limit. Statistics and feedback
reads validate retained JSON, including required fields, enums, timelines and
usage; invalid data returns `500 control_storage_corrupt`, never default state or
an empty success. Database errors return `503 control_storage_unavailable`.

## Build and test

```powershell
dotnet restore SemanticDataNexus.ControlApi.sln
dotnet format SemanticDataNexus.ControlApi.sln --verify-no-changes --no-restore
dotnet test SemanticDataNexus.ControlApi.sln --configuration Release --no-restore
dotnet publish src\ControlApi\ControlApi.csproj --configuration Release --no-restore
docker build --file Dockerfile .
```

The package-local CI workflow runs for changes under `services/control-api/**`
and the shared HTTP contract document. It supplies a disposable PostgreSQL 16
service and `CONTROL_API_TEST_POSTGRES`, runs the complete memory/API/Postgres
suite, publishes TRX results and fails if integration tests are missing/skipped.
Integration tests create and drop uniquely named schemas in that test database;
**never point the test variable at production**. For a local machine without
PostgreSQL, run only the existing memory/API tests with
`--filter FullyQualifiedName!~PostgresRepositoryTests`; this is not equivalent
to passing the required PostgreSQL CI gate.

## Microsoft references

- [Microsoft identity web API](https://learn.microsoft.com/entra/identity-platform/scenario-protected-web-api-app-configuration)
- [ASP.NET Core authentication and authorization](https://learn.microsoft.com/aspnet/core/security/authentication/)
- [Problem Details and exception handling](https://learn.microsoft.com/aspnet/core/fundamentals/error-handling)
- [Rate limiting](https://learn.microsoft.com/aspnet/core/performance/rate-limit)
- [Health checks](https://learn.microsoft.com/aspnet/core/host-and-deploy/health-checks)
- [Forwarded headers](https://learn.microsoft.com/aspnet/core/host-and-deploy/proxy-load-balancer)
- [HTTP client factory](https://learn.microsoft.com/dotnet/core/extensions/httpclient-factory)
- [OpenTelemetry with ASP.NET Core](https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-enable)
