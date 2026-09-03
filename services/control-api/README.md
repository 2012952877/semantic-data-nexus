# Control API

Typed ASP.NET Core 8 control-plane/BFF boundary for identity, run metadata,
cancellation, structured feedback, statistics, and a future semantic backend.
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
A duplicate create first reconciles by `RunId`, then repeats the idempotent
backend start with the same `RunId` only when the backend definitively reports
it missing. Only a definitive backend rejection marks the run failed.

Start, reconciliation, status refresh, and cancellation hold a per-`RunId`
dispatch lease in M0 so cancellation cannot lose to a late start. Cancellation
persists a monotonic generation plus `Pending`/`Delivered` ownership. Failed
delivery remains retryable, while a delivered generation is not posted again.
Active backend observations update projection details without replacing local
`CancelRequested`; a terminal observation resolves it and also makes a racing
delivery acknowledgment idempotent. Feedback uses `submissionId` for
idempotency and `expectedRunVersion` for optimistic concurrency. Feedback is
structured to avoid an unrestricted text/prompt field.

## Configuration

`config\production.env.example` lists the environment variable boundary with no
credential values. Prefer deployment-time configuration and managed platform
identity. Do not place tokens, connection strings, or credentials in files.

- `AzureAd`: Microsoft.Identity.Web JWT bearer settings.
- `SemanticBackend`: base URI and a bounded 1-30 second timeout. Create calls
  have no transport-level retry. Control-plane reconciliation uses the stable
  `RunId` before an explicit repeat dispatch. Responses are rejected before
  persistence when IDs, states, timestamps, usage, diagnostics, stages, or
  nodes violate the bounded contract.
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

`IRunRepository` isolates the API from storage. `InMemoryRunRepository` is the
M0 implementation and is process-local. A PostgreSQL adapter should preserve the
same aggregate semantics:

- unique `(created_by, client_request_id)` and `(run_id, submission_id)` keys;
- a monotonic `version` column checked in `UPDATE ... WHERE version = ...`;
- one transaction for run mutation plus feedback insertion;
- finite-state transition validation before update;
- persisted start-dispatch and cancellation-delivery ownership suitable for an
  outbox worker, with generation-checked compare-and-swap claiming in a
  multi-replica adapter (the in-process dispatch lease is not the distributed
  lock for a PostgreSQL implementation);
- structured JSON only for bounded stages, nodes, usage, and diagnostics.

The adapter belongs in a separate persistence project or folder and must not
leak database models into the HTTP or semantic-client contracts.

## Build and test

```powershell
dotnet restore SemanticDataNexus.ControlApi.sln
dotnet format SemanticDataNexus.ControlApi.sln --verify-no-changes --no-restore
dotnet test SemanticDataNexus.ControlApi.sln --configuration Release --no-restore
dotnet publish src\ControlApi\ControlApi.csproj --configuration Release --no-restore
docker build --file Dockerfile .
```

The package-local CI workflow runs for changes under `services/control-api/**`.

## Microsoft references

- [Microsoft identity web API](https://learn.microsoft.com/entra/identity-platform/scenario-protected-web-api-app-configuration)
- [ASP.NET Core authentication and authorization](https://learn.microsoft.com/aspnet/core/security/authentication/)
- [Problem Details and exception handling](https://learn.microsoft.com/aspnet/core/fundamentals/error-handling)
- [Rate limiting](https://learn.microsoft.com/aspnet/core/performance/rate-limit)
- [Health checks](https://learn.microsoft.com/aspnet/core/host-and-deploy/health-checks)
- [Forwarded headers](https://learn.microsoft.com/aspnet/core/host-and-deploy/proxy-load-balancer)
- [HTTP client factory](https://learn.microsoft.com/dotnet/core/extensions/httpclient-factory)
- [OpenTelemetry with ASP.NET Core](https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-enable)
