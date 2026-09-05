# Catalog compiler v1

This is an **additive internal library contract**, not a replacement for `sqg.v0`
or the two M0 compilation modes. A catalog is a scoped, immutable ontology
snapshot. The compiler validates an actual model candidate against that snapshot;
the model cannot supply a physical table, SQL, resolver, credential, or permission.

| Surface | Current boundary |
| --- | --- |
| Catalog/compiler schemas and library | Implemented; synthetic catalog and real loopback HTTP tests |
| Generic typed runtime integration | Implemented in `semantic_backend.catalog_compilation`; real DuckDB execution over synthetic Arrow sources |
| PostgreSQL clarification repository | Implemented; dedicated PostgreSQL integration tests, not a SQLite substitute |
| Public authenticated compiler/clarification routes | Not connected by this change; requires the #32 identity composition root |
| Ask UI, catalog publishing UI, customer connectors | Not implemented here; #35/#36 and connector-specific conformance |
| Live model accuracy or enterprise gateway support | Not evaluated; all model responses in acceptance tests are socket mocks |

## 1. Inputs and trust

`CatalogDocument` (`catalog.schema.json`) contains entity, field, member, metric,
relation, and time-window definitions. It contains **no physical bindings**.
IDs are arbitrary, unique within a snapshot, and not matched against commerce
prefixes. A metric chooses a governed source field and aggregation function.
A relation specifies typed keys and `many_to_one` or `one_to_one` cardinality.
Each field independently declares `member_governed`. A field containing members
must set it to true; filtering all members out of an authorized view never clears
it. Consequently a zero-choice governed field still rejects scalar comparisons.
The flag is part of the immutable catalog digest, not inferred from visible choices.

`ResourceVersion` carries the product/v1 scope, ontology resource ID, positive
revision, and SHA-256. The digest is the UTF-8, ASCII-escaped, sorted-key compact
JSON serialization of the fully normalized `CatalogDocument.model_dump(mode="json")`,
including defaults. `pin_for()` is the reference implementation. `PublishedCatalogs`
is an immutable in-process publication adapter, not a management database.
Its lookup uses `(tenant_id, workspace_id, resource_id, revision)` **and** verifies
the digest. A repository implementation must never replace content at an existing
revision. The compiler independently recomputes the digest after lookup.
The catalog also pins `bindings_sha256`: changing a physical mapping or timestamp
interpretation requires a new catalog revision, including across clarification
resume. The provider sees this digest but never the physical binding contents.

The required `TrustedContext` protocol mirrors `contracts/product/v1/trusted-context`.
It describes server-verified input; constructing an object with those fields is
not authentication. There is no caller-JSON/header-to-trust conversion here.
An injected async `Authorization.require(context, pin, "compiler:query")` must
check verified issuer/subject, current membership, and exact resource permissions
against the authoritative store. It returns five allowlists: entity, field,
metric, relation, and member IDs. Missing context fails before provider use.

Catalog descriptions, labels, synonyms, questions, answers, rejected candidates,
and repair diagnostics remain untrusted data in a JSON user envelope. The fixed
server system policy and schema are separate. Authorization is enforced in code,
not by that policy text. The provider receives only the authorized catalog view;
unavailable policy fields or members block compilation instead of disappearing.

## 2. Compile and validate

The opt-in library entry points are:

```python
await compiler.compile(request, context=verified_context, deadline=absolute_deadline)
await compiler.resume(
    clarification_id, choice_id, revision=answer_step,
    context=verified_context, pin=original_catalog_pin, deadline=absolute_deadline,
)
```

These snippets describe the library signature, not an unauthenticated runnable
server. Construct `CatalogCompiler` with an immutable catalog repository,
authoritative authorization callback, explicitly configured provider,
clarification repository, and reviewed capability-form list. There is no v1
static-provider or configuration-error fallback. `CatalogHTTPProvider` requires
the existing reviewed `ProviderSettings` real mode and credential supplied by the
server composition root. The production origin remains only
`https://api.openai.com/v1/chat/completions`; explicit loopback mock mode remains
test-only. No gateway or arbitrary-URL expansion is included.

| Form | Authoritative checks |
| --- | --- |
| SELECT | Authorized entity and fields, one source per entity, exact types |
| FILTER | Typed scalar comparison; exact resolved member IDs; pinned time-window IDs; required source policies before transforms |
| JOIN | Authorized relation, directed keys, non-fanout topology; no cross join or model-defined key |
| AGGREGATE | Governed metric IDs/functions, groupable fields, output collisions, no aggregation of duplicated dimension metrics, including through subsequent one-to-one joins |
| PROJECT / SORT / LIMIT | Existing typed columns, unique case-insensitive output aliases, exact output schema, bounded positive limit |
| Other operators, SQL, tools, ASK/ACT/SEARCH | Rejected; no implicit write or search authority |

Validation checks topological order, arity, duplicate nodes/edges, disconnected
nodes, branch reuse, semantic constraints, and the exact catalog pin. It does not
silently widen the old six-operator semantic-core validator or existing SQG v0.
Only the listed implemented forms may be advertised; future runtime v1 operators
need their finalized contract and conformance before admission.
Member predicates compare exact sets independent of their ordering, including
required policy predicates. Duplicate member IDs remain invalid.

The initializer matches authorized IDs/labels/synonyms using longest
non-overlapping mentions. Colliding meanings produce server-owned choices rather
than a guessed ID. Multiple possible time fields also require clarification.
Time windows are explicitly published timezone-aware instants; unrestricted
relative-date interpretation, expression metrics, self-joins, fanout joins,
and arbitrary conversational ambiguity are **not** claimed. An unresolved model
refusal/ambiguity without server-owned choices is blocked.

A semantically invalid candidate gets at most one structured repair, with the
same context and pin. Invalid wire JSON/schema, refusals, transport failures and
budget admission failures do not become successful fixture results. Input
admission includes schema/envelope overhead; context, candidate, output, repair,
and total token budgets are bounded. Unknown usage remains `null`, never zero.
The same caller's absolute monotonic deadline covers authorization, provider
queue/HTTP, repair, database locks, and runtime; cancellation propagates.

## 3. Durable clarification

Install the explicit `semantic-api[postgres]` extra and give
`PostgresClarifications` a server-resolved database connection value. Call
`initialize()` with DDL authority during setup; normal runtime needs only DML
on `compiler_clarifications_v1`. No control-api or identity migration is changed.
Credentials are not accepted in a query request or persisted with a clarification.

Every compile request reserves its owner/request-ID/hash before choosing the
direct or clarification branch. The row stores the original question and authorized prompt context, allowed
choices, exact resource pin, owner identity/membership revision, expiry, context
fingerprint, accepted answers, and their responses. The fingerprint includes
the access allowlists, prompt/schema, provider configuration (not credential
value), capability forms and compiler limits. Treat persisted questions as
sensitive application data; deployment encryption, access control and retention
remain operational requirements.

| Transition | Result |
| --- | --- |
| Same owner + request ID + same request | Same persisted clarification/result |
| Same owner + request ID + different request | Idempotency conflict |
| Same ID changes from clarification to direct (or vice versa) | Conflict before provider use |
| Concurrent/repeated direct request with identical hash | One locked generation, then the persisted response |
| First valid answer at current step | Row lock, current authorization, bounded compile, reauthorization, atomic answer/result commit |
| Same step + same answer | Stored response, no second model call |
| Same step + different answer / skipped step | Conflict / revision rejection |
| Multiple ambiguities | Same ID, next answer-step revision, original expiry retained |
| Wrong scope/issuer/subject/principal/membership or pin | No continuation; opaque unavailable result |
| Expired, revoked, changed grants/catalog/configuration | Fail closed; no implicit rebind |
| Cancellation/deadline before transaction commit | Rollback; another client may retry |

Two clients serialize on `SELECT ... FOR UPDATE`. A restarted repository needs
no process-local continuation state. A cancelled direct generation retains an
explicit pending row (`current=null`), its original hash and expiry; a different
question cannot replace it. An explicit same-hash retry may finish that pending
request. This gives idempotent **compiled responses**,
not exactly-once paid inference: a crash after a provider returns but before the
database commits may require another inference on retry. Runtime execution
idempotency/recovery is a separate concern, not implied by answer replay.

Authorization is rechecked before publication. The callback must use live
membership/resource authority, not an old `authorized_at`. Integrating a strict
database-linearized authorization/commit boundary requires the identity
composition root; this internal library does not claim that unconnected API
integration or distributed execution revocation is complete.

## 4. Bind and execute

`semantic_backend.catalog_compilation` accepts a reviewed `CatalogBindings`
snapshot pinned to the catalog and a server-selected resolver. Physical fields
and source objects are never model output. Each SELECT is bound exactly, then a
local typed projection renames physical columns to semantic IDs, preventing
cross-source name collisions without scenario-specific mapping.
The binding-content digest is canonical JSON of
`{"contract_version":"catalog-binding-content/v1","entities":[...]}`. Each ordered
entity entry has `entity_id`, `alias`, `source_type`, `object_name`, a semantic-ID
to physical-name `columns` object, and ordered `utc_naive_fields`. The catalog
pins that digest before its own content digest is computed; the adapter checks
both. Logical field types are pinned in the catalog and must match each binding.

The adapter uses the existing `query-runtime/v0` typed core, pushes down SELECT
only after source capability admission, and executes remaining supported forms
locally. The source wrapper validates Arrow types/schema and governed unique
join keys. Timestamp inputs with a timezone are normalized to UTC-naive instants
for the v0 runtime; naive timestamps require an explicit reviewed UTC binding.
This avoids host/session timezone-dependent boundary comparisons.
Unique keys are checked **after** type and timestamp normalization by a typed
DuckDB GROUP BY using the same execution engine as JOIN, not Arrow distinct or a
Python set. Signed zero therefore cannot double a matched fact. Floating join
keys must be finite; decimal bindings remain unsupported; lossy sub-microsecond
timestamp conversion is rejected. Empty dimension tables remain valid. Source
and key-check tasks are retained and cancelled/drained before the invocation exits.

`execute_catalog` requires the same absolute deadline used by compile/resume,
rechecks current authorization and pin, applies runtime row/byte/memory limits,
and returns the typed table, physical plan, runtime outcome/events/manifest,
provider calls, and catalog/binding fingerprints. Its result store is ephemeral
and private to that invocation; durable product run/history/result integration
is not added here.

## 5. Evidence and regeneration

| Claim | Focused test reference |
| --- | --- |
| Independent energy/laboratory domains; different actual wire graphs | `services/semantic-api/tests/test_catalog_v1.py::test_heldout_actual_wire_candidates` |
| JOIN/time/member/SUM and numeric/member/AVG through DuckDB | `services/semantic-backend/tests/test_catalog_compilation.py::test_heldout_natural_language_wire_to_typed_duckdb_result` |
| Unknown/unauthorized semantics, type/shape/aggregation/capability failures, no SQL escape | `test_catalog_v1.py` negative parameterizations |
| One bounded repair and injection data separation | `test_catalog_v1.py::test_wire_repair_injection_data_and_same_catalog_pin` and existing `test_structured_provider.py` |
| Restart/two-client replay, conflict, cancellation rollback, revocation, pin/expiry and lock deadline | `services/semantic-api/tests/test_catalog_postgres.py` (requires real disposable PostgreSQL) |
| Runtime schema/key/time/budget/cancellation | `services/semantic-backend/tests/test_catalog_compilation.py` |
| Empty-member grants, transitive fanout, order-independent predicates with duplicate rejection | `services/semantic-api/tests/test_catalog_review_regressions.py` |
| Signed zero, non-finite/decimal/time boundaries, denied raw member access, safe one-to-one metric results | `services/semantic-backend/tests/test_catalog_review_regressions.py` |
| Direct/clarification request-ID conflicts, two-client direct replay and cancelled reservations | New direct-request cases in `services/semantic-api/tests/test_catalog_postgres.py` |

`examples.json` contains clean-room synthetic acceptance data, not runtime query
templates. Model responses are mocked at a real HTTP socket; no model was called
and no live accuracy/latency/cost claim follows from these tests.

From the repository root, after installing the local package, regenerate schemas
and example pins with `python -m semantic_api.catalog_v1.export_contracts`.
The generator is deterministic; `candidate.schema.json` is the closed structured
response schema. Existing M0 release-gate workflows remain the regression baseline.
