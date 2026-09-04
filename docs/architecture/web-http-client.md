# Web HTTP client

The Web workbench has two explicit client modes. `mock` remains the default and retains the Web Locks-backed browser history. `http` connects only to the authenticated control-plane BFF described by `docs/architecture/http-contracts.md` in the BFF integration change.

## Configuration and trust boundary

- `VITE_NEXUS_CLIENT` accepts only `mock` or `http`.
- HTTP mode requires `VITE_NEXUS_BASE_URL`, an HTTP(S) URL without credentials, query parameters, or a fragment. Browser use must resolve to the Web page's origin; production hosts reverse-proxy `/api/v1` to the BFF because PR #22 deliberately enables no CORS. Plain HTTP is accepted only for loopback development.
- Local Vite development may set the server-only `NEXUS_PROXY_TARGET`. It configures `/api` forwarding and is not exposed through `import.meta.env`.
- `VITE_NEXUS_DEV_SUBJECT` and `VITE_NEXUS_DEV_ROLES` are optional, non-secret local-development values. The client sends them as `X-Dev-Subject` and `X-Dev-Roles` only to a loopback BFF.
- Bearer credentials are supplied only through the injected asynchronous `tokenProvider(AbortSignal)` seam. The production host can define `window.semanticNexusTokenProvider` before the application module loads; tokens are never read from `VITE_*` variables or persisted.
- Every successful BFF response is runtime validated before mapping into UI state. Problem responses and all BFF text use text rendering; the UI never interprets response strings as HTML.

## Create contract

`POST /api/v1/runs` sends:

```json
{
  "clientRequestId": "request-001",
  "workload": "regional-sales",
  "question": "Compare regional revenue",
  "evaluationClock": "2026-09-04T05:15:50.492Z",
  "evaluationTimezone": "Asia/Shanghai",
  "compilationMode": "regional_quarterly_profit",
  "executionMode": "thread",
  "outputMode": "normal"
}
```

Questions are limited to 1-4,000 visible characters. `evaluationClock` always contains `Z`; `evaluationTimezone` is validated as an IANA timezone. The client supports the typed BFF compilation modes `regional_quarterly_profit` and `monthly_regional_comparison`, execution mode `thread`, and output modes `normal` and `stream`. The current workbench defaults are `regional-sales`, `regional_quarterly_profit`, `thread`, and `normal`.

## Summary contract

Create, `GET /api/v1/runs/{id}`, `POST /api/v1/runs/{id}/cancel`, and the `items` in `GET /api/v1/runs` use the BFF `RunMetadata` shape:

- identity and request context: `id`, `clientRequestId`, `workload`, `question`, `evaluationClock`, `evaluationTimezone`, `compilationMode`, `executionMode`, and `outputMode`;
- ownership and state: `createdBy`, typed `state`, `cancellationDelivery`, `cancellationGeneration`, and `version`;
- time: `createdAt`, `updatedAt`, nullable `startedAt` and `completedAt`, plus optional serialized `duration`;
- execution: stage summaries with nested node summaries, token usage, and summary diagnostics.

Run IDs use `run_` followed by 32 lowercase hexadecimal characters. The typed BFF state set is `StartPending`, `DispatchUnknown`, `Queued`, `Starting`, `Running`, `CancelRequested`, `Cancelled`, `Succeeded`, and `Failed`. Cancellation delivery is `NotRequested`, `Pending`, or `Delivered`.

The Web maps pre-start states to `queued`, active and cancel-requested states to `running`, and `Cancelled` to the existing UI state `canceled`. It derives elapsed and stage durations from the typed timestamps, validates token totals, and projects BFF stage identifiers onto the workbench's five-stage ledger.

`GET /api/v1/runs` returns `{ "items": RunMetadata[], "count": number }`; `count` must equal `items.length`.

## Detail contract

`GET /api/v1/runs/{id}/detail` returns a separate typed semantic detail:

```json
{
  "runId": "run_00000000000000000000000000000001",
  "question": "Compare regional revenue",
  "sqg": {
    "version": "0.1",
    "intent": "Compare regional revenue",
    "ontology": "regional-sales",
    "resolvedMembers": ["sales.region"],
    "metrics": ["net_revenue"],
    "dimensions": ["sales.region"],
    "filters": [{ "field": "quarter", "operator": "equals", "value": "2025-Q2" }],
    "policyChecks": ["governed"]
  },
  "physicalNodes": [{
    "id": "aggregate-region",
    "kind": "AGGREGATE",
    "label": "Aggregate by region",
    "plainLanguage": "Groups governed sales by region.",
    "inputs": ["regional-sales"],
    "outputFields": ["region", "net_revenue"]
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
    "resultId": "result-001",
    "runId": "run_00000000000000000000000000000001",
    "nodeId": "aggregate-region",
    "storage": "inline",
    "uri": "results/run/result-001",
    "rowCount": 1,
    "byteCount": 128,
    "checksum": "sha256:synthetic",
    "committedAt": "2026-09-04T05:15:52.332Z"
  },
  "lineage": {
    "version": "query-runtime/v0",
    "runId": "run_00000000000000000000000000000001",
    "nodes": [],
    "edges": []
  },
  "diagnostics": []
}
```

The client enforces the BFF bounds for SQG collections, up to 1,000 nested status nodes per stage, physical nodes, columns, inline rows, lineage nodes and edges, and diagnostics. Result rows are positional arrays and each JSON scalar is validated against its column `dataType`; null is accepted only for nullable columns. Supported physical kinds are `SOURCE`, `SELECT`, `FILTER`, `AGGREGATE`, `PIVOT`, `DERIVE`, `PROJECT`, `SORT`, `LIMIT`, and `JOIN`.

The detail `runId` and `question`, nested manifest and lineage run IDs, diagnostic run IDs, result/manifest presence, and row counts must agree with the validated summary. A successful zero-row detail maps to the UI-only `empty` state. Array rows are converted to key-addressed UI rows without evaluating response content.

## Polling, cancellation, and failures

After create, the client polls `GET /api/v1/runs/{id}/semantic-status` with bounded exponential backoff so the BFF refreshes and persists semantic backend state. `GET /api/v1/runs/{id}` remains a passive metadata lookup. Defaults are 200 ms initial delay, 2 seconds maximum delay, and a 30-second total deadline. A successful or failed terminal summary is followed by `/detail` within the same deadline.

Queued, active, and `Cancelled` direct lookups use BFF summary metadata alone. Cancellation never requires semantic detail because a run can be validly cancelled after the BFF reconciles a backend `404`.

Each otherwise independent BFF request has a 10-second deadline. The remaining deadline is propagated through token acquisition, fetch, and response-body consumption using `AbortSignal`. Allowed `404` lookup responses abort and discard their body before mapping to an absent run.

The client distinguishes request, configuration, network, HTTP, invalid-response, and timeout failures. Non-success responses may use RFC 7807 Problem Details with `title`, optional `detail`, `code`, and `status`; malformed problem bodies still produce an HTTP failure without reflecting arbitrary response content.

Vitest and the primary Playwright suite use a deterministic real HTTP stub that implements this wire contract. A separate Chromium suite keeps the existing native Web Locks lease-fencing and expiry coverage for default Mock mode.
