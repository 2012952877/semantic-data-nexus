# Semantic Compiler API

A clean-room Python 3.12 service that resolves governed semantic concepts and compiles a
business question into a validated, typed semantic query graph (SQG). It never generates,
accepts, or executes SQL.

## Security boundary

- Questions and ontology/catalog values are untrusted data, never instructions.
- Providers receive a bounded, structured `compile-context.v0` object rather than concatenated
  prompt text. The context is an immutable serialized snapshot separate from the authoritative
  initialization result used for validation.
- Provider output must match `sqg.v0`, exact ontology membership, query policy, DAG, operator,
  column-flow, grain, and result-schema rules.
- Negation is normalized and detected conservatively within bounded mention clauses; unsupported
  negative member, time, entity, field, or metric requests fail closed with typed diagnostics.
- Monthly comparison derives concrete current and previous calendar-month windows from the
  evaluation clock, filters exactly their combined range, and binds pivot aliases to month starts.
- Validation failures permit exactly one provider repair using only the rejected candidate and
  stable machine-readable diagnostics. Failure remains explicit.
- Static deterministic compilation remains the default local/dev provider. Explicit server
  configuration enables real structured model calls; failures never fall back to fixtures.
  Neither provider generates SQL, executes queries, or accesses source data.
- Logs contain request metadata and exception types, not raw questions, catalogs, candidates,
  model payloads, secrets, or exception text.
- Provider calls run in isolated asyncio tasks behind monotonic deadlines; late results are always
  rejected and cancellation is requested. Python cannot forcibly terminate cancellation-resistant
  in-process code, so production adapters must delegate blocking SDK work to a killable worker
  process or use an SDK transport with enforceable network deadlines.

`shared_contract_adapter.py` is the explicit seam for replacing package-internal v0 request and
response models with foundation contracts without changing initializer, compiler, or validator
logic.

## Structured model provider (M1)

This is a genuine model-provider boundary for the **existing two compilation modes**, not
generalized catalog-driven compilation or a complete commercial product. Issue #33 owns
generalized compilation. No live model verification has been executed for this implementation;
all protocol tests use synthetic local HTTP servers. No deployment or billable resources change.

`SemanticCompiler.from_environment()` and `ProviderRuntime` are the reusable server-owned
configuration/factory/lifecycle seam, shared by the API, CLI and semantic-backend. No caller can
provide an endpoint, credential, model or policy override. In real mode even the legacy
`provider_selection: "static"` default routes to the configured model; it is not a downgrade
switch. In static mode an explicit `openai_compatible` selection fails with
`PROVIDER_NOT_CONFIGURED`. Existing requests and unset-environment behavior are unchanged.

Only non-streaming OpenAI Chat Completions with a strict `json_schema` response format is
implemented. The production endpoint allowlist contains exactly
`https://api.openai.com/v1/chat/completions`. Supply an exact model snapshot ID supporting this
protocol, for example `gpt-4o-2024-08-06`; the response model must match, not silently resolve to
another deployment. `deployment` metadata is null because this variant has no deployment ID.
Azure OpenAI, Responses API, private endpoints, arbitrary compatible gateways, tools and
reasoning-specific options are **not supported**. Adding one requires separate protocol and
DNS/egress validation, not loosening this allowlist.

| Server environment variable | Default / constraint |
| --- | --- |
| `SEMANTIC_COMPILER_MODE` | `static` or `openai_compatible` |
| `SEMANTIC_COMPILER_MODEL` | Required exact model ID in real mode |
| `SEMANTIC_COMPILER_CREDENTIAL_ENV` | Name of a server environment variable holding a provisioned bearer credential; never the credential itself |
| `SEMANTIC_COMPILER_ENDPOINT` | Exact allowlisted production URL above |
| `SEMANTIC_COMPILER_ALLOW_LOCAL_MOCK` | `false`; explicit `true` additionally permits only `http://127.0.0.1:<port>/v1/chat/completions` |
| `SEMANTIC_COMPILER_TIMEOUT_SECONDS` | 5; finite, positive, at most 120 seconds per call including queue/body |
| `SEMANTIC_COMPILER_MAX_OUTPUT_TOKENS` | 2048; 1-16384, sent as `max_completion_tokens` |
| `SEMANTIC_COMPILER_MAX_INPUT_TOKENS` | 32768; 1024-131072, conservative admission and reported-usage ceiling |
| `SEMANTIC_COMPILER_MAX_REQUEST_BYTES` | 65536; 1024-262144 UTF-8 bytes |
| `SEMANTIC_COMPILER_MAX_RESPONSE_BYTES` | 131072; 1024-1048576 raw HTTP body bytes |
| `SEMANTIC_COMPILER_MAX_CONCURRENT_CALLS` | 4; 1-16 per process |

Provision credentials through deployment secret injection using a reference name outside the
`SEMANTIC_COMPILER_` settings namespace. Unknown settings, incomplete real configuration and
non-allowlisted URLs fail closed: readiness is 503 and compilation reports
`PROVIDER_CONFIGURATION`, never a success fixture. Readiness checks configuration and lifecycle
only, **not** model availability, credential validity at the remote service or model access.
It never sends billable health checks. Settings are read once; restart to rotate/reconfigure.

The request has one original server-owned system policy and one serialized user-data envelope.
Question/catalog/repair content never becomes a system message. The response schema closes
every object, requires every field and narrows datetime ranges to `start`/`end_exclusive`.
Local validation enforces the same schema before authoritative semantic validation; only a
schema-valid but semantically invalid candidate gets the existing single repair attempt.

Each call admits the complete serialized ASCII-escaped request (including schema) only when
its byte length plus a conservative 1024-token framing reserve fits the input ceiling. This
is deliberately stricter than a tokenizer estimate, not reported usage or a billing quote.
Reported usage must contain nonnegative integer counts, consistent totals and valid details.
There are at most two calls, no SDK/HTTP retries, and an output-token ceiling on each call.
Rate limits, authentication failures, redirects, network failures, malformed/duplicate JSON,
extra payloads, tools, refusals, truncation and over-budget responses terminate with stable
`PROVIDER_*` diagnostics. Compressed responses are rejected rather than decompressed unboundedly.

`token_metadata.provider_calls` records model, phase, outcome and reported tokens for each
attempt. Unknown usage stays null, including aggregate totals when a failed repair has unknown
usage; successful compile usage remains visible in the per-call entries. Cancellation is
propagated, never converted into success. The async response/client are owned by each call and
closed on cancellation, timeout or shutdown. Environment proxies and redirects are disabled.
Transport logs for these calls are suppressed even at DEBUG so headers/payloads cannot leak.
Safe diagnostic codes contain no raw response body, endpoint, credential or exception text.

For a separately authorized live smoke, provision a credential reference and set the real-mode
environment variables on an isolated server, then run the existing CLI with a synthetic question.
There is no automatic live test or model discovery. Do not enable real mode on the current
hosted demo as part of this PR. Live integration and operational acceptance remain a separate
milestone after review.

Protocol references: [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
[Chat Completions request types](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/completion_create_params.py),
[response types](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion.py),
and [usage types](https://github.com/openai/openai-python/blob/main/src/openai/types/completion_usage.py).
The implementation uses httpx directly to enforce raw-body limits and exact envelope rejection;
it does not rely on permissive SDK response parsing or retry defaults.

## Run

```powershell
cd services\semantic-api
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\uvicorn semantic_api.api:app --host 127.0.0.1 --port 8000
```

OpenAPI is available at `http://127.0.0.1:8000/docs`.

## CLI

```powershell
semantic-api "Show regional quarterly profit for 上季度"
semantic-api "Compare monthly regional profit" --mode monthly_regional_comparison
```

The command prints a JSON response containing resolution diagnostics, the selected synthetic
semantic context, candidate SQG, validated normalized SQG, and non-secret timing/token metadata.
A nonzero exit code means clarification or compilation failure.

## Test and quality

```powershell
cd services\semantic-api
.\.venv\Scripts\pytest
.\.venv\Scripts\ruff format --check .
.\.venv\Scripts\ruff check .
.\.venv\Scripts\mypy
.\.venv\Scripts\python -m build
```

All fixtures are synthetic. Compilation does not determine whether runtime data exists; a valid
SQG remains valid even if a later execution layer returns no rows.

## API summary

- `GET /health/live`
- `GET /health/ready`
- `POST /v1/initialize`
- `POST /v1/compile`

Requests require a timezone-aware evaluation clock and an explicit IANA timezone. Errors use a
stable Problem Details-like envelope and return the request ID in both the body and
`X-Request-ID`.
