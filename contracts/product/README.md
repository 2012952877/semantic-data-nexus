# Product contracts v1

This subtree is the executable scope and cross-service contract foundation for
#29, part of #28. It is independent of `contracts/v0`; no v0 schema, API,
authentication middleware, provider, control repository or deployment is changed.
The original schemas below are not a reproduction of a reference product.

## Inventory is not implementation

`v1/capabilities.json` covers 20 observed standard domains, four Ask modes,
26 observed operator families, enterprise foundations and commercial integrations.
Observation summaries retain only user-visible capability evidence from a
sanitized report. No bundle, internal schema, URL, identifier, payload, prompt
or asset from that reference is included. Backend algorithms and actual deployed
connector/provider coverage remain unknown.

Every capability has a stable ID, independent description, category, observation
source/confidence, current state, M0 gap, dependencies, primary owner surface,
issue, milestone, positive/negative/integration cases and evidence references.
Case identities are `<capability-id>/<positive|negative|integration>`; IDs must
not be reassigned to unrelated meanings. A changed scope needs a new ID or an
explicit versioned migration, not a silent meaning change.

| State | Meaning and promotion evidence |
| --- | --- |
| `planned` | Required scope not yet implemented in full; `current_gap` preserves any narrower M0 behavior |
| `implemented` | Code or schema exists for the exact described scope, with resolvable positive/negative pytest cases and implementation/test evidence; does not imply a service integration |
| `integrated` | In addition, a resolvable integration case and scoped integration evidence connect the applicable UI/API/storage/authorization/failure path |
| `verified` | In addition, a verification record for an exact candidate revision, environment and workload establishes the acceptance outcome; no universal certification |
| `unsupported` | Explicitly not executable under the current contract; a reason and future acceptance remain visible |

Only the five #29 contract foundations currently claim `implemented`. All standard
workflows are `planned`; ACT is `unsupported`. Existing M0 operator subsets are
recorded in `current_gap`, not erased or promoted to complete standard conformance.
Operator IDs normalize names to lowercase with underscores replaced by hyphens.
`operator.ask` and `standard.ask` are different capabilities.

`acceptance.*.test_ref: null` explicitly means **no executable acceptance yet**,
not pass or skip. Test references use repository-relative
`path/to/test_file.py::test_function`; evidence references name repository files.
References and selectors must resolve locally. Do not invent placeholder test
paths or count a template, installed package or transport mock as live evidence.
Observation confidence and implementation state are deliberately separate axes.
Integration and verification evidence must also supply a `run` record with exact
commit, command, environment, passed outcome and timestamp; `ref` must resolve to
a JSON file with that same record, not merely a test source file.

The validator rejects incomplete required coverage, duplicate IDs, dangling
source/evidence/dependency references, cycles, invalid issue/milestone mapping,
unsupported delivered claims and loss of ACT's explicit authorization rule.
It also prevents a delivered capability depending on an undelivered capability.
Issue milestone is a workstream assignment, not a promise that every downstream
workflow is integrated in that milestone.

## Contract surfaces

All schemas are JSON Schema Draft 2020-12 with local URN identifiers. They are
resolved offline by `validation.py`; consumers must enable format checking and
must not fetch schemas from arbitrary network locations. Unknown properties,
unknown versions, malformed identifiers and unsupported enums fail closed.
Timestamp values use the RFC 3339 profile with a timezone, up to six fractional
digits and no leap seconds. The offline checker validates calendar dates without
relying on jsonschema's optional date-time dependency.

| Contract | Core fields | What it does not establish |
| --- | --- | --- |
| `trusted-context/v1` | `scope`, `principal`, `authentication`, `membership` | A signature, an authenticated channel, actual membership or access enforcement |
| `resource-version/v1` | `scope`, `resource_kind`, `resource_id`, `revision`, `content_sha256` | Storage persistence, hash correctness or permission to read content |
| `audit-envelope/v1` | append operation, event/stream identity, sequence/parent, scope, actor, decision reference, resource version, action/outcome, trace | Tamper-proof storage, an authorization decision or delivery of audit logging |
| `usage-envelope/v1` | append operation, event/stream identity, sequence/parent, scope, producer/run/meter/unit, exact quantity, measurement/correction | Collection, quota enforcement, pricing, invoices or authorization to charge |
| `common` definitions | bounded IDs, scope, OIDC principal, versioned opaque secret reference | A secret broker or runtime capability discovery |

### Trusted context: authority precedes serialization

`scope = {tenant_id, workspace_id}` is chosen from authorized server-side membership,
not from arbitrary tenant/workspace request headers. A client selection is only a
hint until membership is resolved for the authenticated principal.

The trusted issuer is configured independently of the request. Middleware must
verify the OIDC token signature/keys, issuer allowlist, audience, lifetime and
appropriate flow checks before resolving the stable **issuer + subject** pair to
`principal_id`. Display name or email is not identity. It then checks active
tenant/workspace membership, resource permissions and current revocation state,
and records a membership ID/revision and authorization time for that exact scope.

The serialized `authentication.method = "oidc"` is a format discriminator, not an
attestation. A caller can forge any JSON, including a perfectly valid context.
Consumers must accept this object only from the trusted verifier over an
authenticated service boundary and must revalidate according to the revocation
policy; they must never bind public request JSON directly into trusted context.
`validate_context` checks only shape and time ordering against an explicit clock.
No OIDC verification, membership database or authorization middleware is delivered here.

#32 owns integration and must cover revoked users/groups, wrong issuer/audience,
forged scope headers, cross-workspace IDs, SSE, results, exports and administrative
APIs. Contract validation alone must never be used as proof of tenant isolation.

### Immutable resource versions

The identity tuple is `(tenant_id, workspace_id, resource_kind, resource_id, revision)`.
`revision` is a positive, server-allocated version, distinct from schema version.
The digest identifies the exact immutable stored content bytes, excluding this
identity envelope; a storage adapter must specify its serialization before hashing
and verify the digest on retrieval. These synthetic digests are shape examples,
not claimed hashes of any stored object.

Publishing or rollback creates a new revision; it must not overwrite bytes under
an existing identity. Resource grants and optimistic concurrency remain external
requirements. Mutable labels and a selected "current" revision are not immutable
identities. #31/#35 own transactional allocation, publication and compatibility.

### Append-only audit and usage

Streams are scoped by `(contract_version, tenant_id, workspace_id, stream_id)`.
Start at sequence 1 with null `previous_event_id`; later records reference the
preceding event and increment sequence exactly once. Event identity is unique in
the stream, with nondecreasing recorder timestamps. Adapters must perform atomic
expected-head append, enforce uniqueness and reject update/delete. An exact retry
may return the already committed event but must not append it or charge it again;
the same event ID with different content is a conflict.

`validate_append_sequence` checks a **complete stream from sequence 1**. It is not
a database or a verifier for arbitrary partial pages. Scope consistency, audit
resource scope, duplicate IDs, ancestry and usage corrections are checked across
records because JSON Schema cannot enforce these cross-record invariants.
Concurrent writes, append-only database permissions, retention, restore and
tamper resistance need #34/#38 adapter and operational evidence.

Audit records contain bounded metadata, not raw request/response bodies, prompts,
SQL, results or secrets. Actor identity and `authorization_ref` must be resolved
by the trusted producer, never invented from a failed request. This v1 envelope
is for events with a known principal and versioned resource; unauthenticated
request rejection and missing-resource diagnostics need a separately reviewed
redacted operational format, not fabricated audit actors/resources.

Usage quantity is an exact decimal **string**, never a JSON float. A measurement
is nonnegative. An adjustment is a signed delta referencing an earlier measurement
in the same stream, scope, producer, run, meter and unit; it does not rewrite the
measurement and cannot target another adjustment. A producer must assign stable
event IDs and truthful units. Meter-specific reconciliation, nonnegative final
totals and reservation/release policies are #38 work, not guarantees of this schema.

Secret references use `{store, name, version}` aliases, not inline values, URLs,
connection strings or arbitrary filesystem paths. Resolution is a separately
authorized operation; opaque names themselves cannot prove safe access.

## Synthetic examples and executable checks

`v1/examples.json` contains complete positive documents. Each negative example
names a positive `base` and one explicit `set` or `remove` at a list-of-keys path.
The test runner deep-copies and applies that mutation to produce a complete
invalid document. This avoids maintaining divergent copies of shared synthetic
identities. Each case has a stable ID and no sensitive values. Unknown mutation
operations fail the fixture runner rather than silently becoming no-ops.

From the repository root, using the existing root development dependencies:

```powershell
python -m pytest tests\test_product_contracts.py tests\test_schemas.py
python -m semantic_core validate examples\regional-quarter-profit.json --ontology examples\retail-ontology.json
```

The existing root CI runs `python -m pytest`, so these tests are collected without
a new workflow or test runner. Root contracts/tests have no configured Ruff/mypy
gate; service-specific gates remain owned by their respective packages.
`validation.py` is an offline conformance helper using the existing `jsonschema`
development dependency, not an import into production service code.

Executable checks validate structure, local evidence availability and synthetic
invariants. They cannot establish that a referenced test was actually run against
a release or that a handwritten verification report is true. Promotion review
must inspect the exact commit, command, environment, outcome and limitations of
integration/verification evidence; code-only references are insufficient.

## Compatibility and dependent work

The namespace is `urn:semantic-data-nexus:product:<name>:v1`; payload versions do
not carry the `product` prefix. Existing `sqg/v0`, run/result APIs and language
models remain unchanged. Breaking schema or identity semantics require a new
version and explicit migration; adding required fields is breaking for these
closed objects. No consumer is silently upgraded.

| Existing M0 surface | New contract boundary / migration entry |
| --- | --- |
| Development identity and caller-selected correlation headers | #32 must create verified context from OIDC and stored membership; `X-Correlation-ID` is only tracing, never `authorization_ref` or identity |
| Control run `Version`, cancellation `ExpectedVersion`, feedback `ExpectedRunVersion` | Mutable optimistic-concurrency counters remain unchanged; do not rename them to immutable content `revision` or schema `contract_version` |
| Control run IDs use `run_` plus lowercase hexadecimal | Keep existing DTO validation; product IDs are a broader opaque format, not permission to relax the v0 API |
| `run-event/v0` has per-run sequence starting at 0, `emitted_at` and a domain payload | Do not reinterpret it as audit/usage. #34/#38 must explicitly map selected redacted metadata into a separately scoped append stream starting at 1 |
| Existing v0 payloads have no product `scope` or membership fields | #32 introduces a versioned adapter/API boundary and stored identity mapping; never default legacy data to an arbitrary tenant or infer membership from an owner string |

There are no new HTTP audit or workspace headers in this increment.
Migration must preserve v0 readers, define authorized legacy-data ownership and
backfill rules explicitly, and test compatibility, conflict handling and rollback
before enabling the new consumers. #31 public DTOs are not expanded here.

#30 provides bounded model transport, not a declaration of live provider support.
#31 owns PostgreSQL control storage, not automatic adoption of this context.
#32 wires identity; #33 wires generalized compilation; #34 wires durable runtime;
#35–#37 deliver standard workflows and conformance; #38 supplies operational and
commercial evidence. See [roadmap](../../docs/roadmap.md) and
[ADR-0006](../../docs/adr/0006-product-contract-foundations.md).
