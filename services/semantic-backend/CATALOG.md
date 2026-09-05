# Versioned catalog query composition

The opt-in catalog service exposes `POST /v1/catalog/queries` with the existing
`catalog-compile/v1` request, and
`POST /v1/catalog/clarifications/{id}/answers` with a `catalog-answer/v1` body
(`catalog`, `revision`, `choice_id`). Both require the reviewed request-bound
service assertion and `compiler:query`; anonymous backend calls remain 401.
Only the verified `TrustedContext` is used for tenant/workspace/principal identity.

`CatalogAuthorization` performs typed contract conversion into the existing
`PostgresAuthorization.guard`; it contains no new identity authority. The guarded
compiler uses that real same-connection transaction boundary for preparation and
result commitment. Model calls occur outside guards. A best-effort watcher
cancels in-flight work when current membership/resource grants change; final
guards still deny publication if cancellation arrives too late.

An execution result is a `catalog-query-result/v1` response. It includes the
validated compilation (readonly metadata, never accepted as an execution input),
a typed `ResultSet`, and catalog/binding/runtime provenance. Runtime result
reservations and completed responses are persisted in
`compiler_catalog_results_v1`, scoped through the guarded compiler record. Same
compiled requests replay the result rather than executing a source twice.
Expired uncertain executions remain `RUNTIME_OUTCOME_UNKNOWN`; they are not
automatically retried. This small result reservation is not the #34 durable
worker/event/history system.

A single catalog-only ASGI receiver preserves the request body used by service
signature verification and observes disconnect messages. Disconnect races cancel
the owned operation with bounded settlement; retained late work cannot enter or
commit an authorization guard. A result committed before disconnect remains a
valid terminal result. The BFF forwards request cancellation rather than treating
an aborted browser request as a successful operation.

Timestamp source strings reject nonzero sub-microsecond fractions before Python
datetime parsing, while redundant zeroes and valid offsets remain lossless.
Public integer results use the shared JSON safe-integer range. Values outside
that range are not rounded or cast to float: `RESULT_INTEGER_OUT_OF_RANGE` is
recorded as an authorized terminal failed result. Repeating the same request
replays that typed failure rather than leaving an indefinite in-flight reservation.

The public composition uses the merged `query-runtime/v1` and `PluginRuntime`.
Source fragments retain the reviewed v0 SOURCE/SELECT grammar; local operations
are explicitly typed `OperatorSpecV1`. The compiler still advertises only its
mapped SELECT, FILTER, JOIN, AGGREGATE, PROJECT, SORT and LIMIT forms. Availability
of additional runtime operators does not authorize the model to emit them.

## Server configuration

No default is changed. Without `SEMANTIC_NEXUS_CATALOG_CONFIG`, catalog queries
are unavailable and M0 routes/modes remain unchanged. An explicit configuration
file uses `catalog-server/v1` with `entries`: each entry has an immutable
`document`, pinned `bindings`, and bounded synthetic source `rows` keyed by the
binding source alias. There are no question-to-graph templates in the service.
This file loader registers explicitly labeled synthetic Arrow sources; customer
resolver management is not claimed by that loader.

The generic provider has a **separate** namespace:
`SEMANTIC_CATALOG_PROVIDER_MODE`, `_MODEL`, `_ENDPOINT`, `_CREDENTIAL_ENV`, and the
other reviewed `ProviderSettings` names. Real mode requires `openai_compatible`;
there is no static candidate fallback. `SEMANTIC_COMPILER_*` remains reserved for
the existing M0 provider configuration. The exact OpenAI origin allowlist and
explicit loopback mock switch are unchanged. Credentials are resolved only from
the named server environment reference and never enter a question or catalog.

Startup initializes the compiler/result tables using the configured shared
identity PostgreSQL database. Runtime state transitions use the authority-owned
connection. The existing control-plane identity migrations must already exist.

## Evidence and limits

`integration/catalog_public.py` uses the actual merged identity schema,
`PostgresAuthorization`, signature-verifying ASGI HTTP middleware, the fenced
compiler, real loopback model HTTP, and PluginRuntime/DuckDB. It covers two
independent domains, cached execution, clarification/resume/conflicts, anonymous
and foreign-scope rejection, in-flight revocation, and an actual shared/exclusive
PostgreSQL publication race. Model responses and source rows are synthetic.

These backend tests are not evidence of a completed browser login/BFF path.
Keycloak/browser and BFF composition have separate acceptance tests. Hosted CI
for new commits is currently blocked before execution by the account billing
gate; local PostgreSQL evidence is recorded separately, not called a green CI run.
