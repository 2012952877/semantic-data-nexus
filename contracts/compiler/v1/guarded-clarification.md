# Guarded clarification adapter

`GuardedCatalogCompiler` is an opt-in adapter around the reviewed catalog
compiler core. It consumes `AuthorizationGuard.guard(context, pin, permission,
deadline=...)`; it does not implement identity, memberships, signatures or grants.
The public composition must supply the reviewed backend authority.

## Connection and transaction boundary

The guard yields readonly `connection` and `access` attributes. Access has five
frozensets (entity, field, metric, relation, member IDs); it need not be a Pydantic
model. The adapter converts those attributes and uses only that decision's
`psycopg.AsyncConnection[tuple[object, ...]]` for catalog and store operations.
Neither `GuardedClarifications` nor `GuardedCatalogCompiler` connects or commits.

The lock order is the authority's shared advisory gate, optional immutable catalog
pin lock, request/clarification row, then attempt row. The supplied catalog reader
accepts the same connection. `PublishedCatalogGuardAdapter` wraps immutable
in-memory snapshots without I/O; a SQL reader must perform its read/key-share on
the supplied connection. The core's pure `_context_from_catalog` helper does
not call `authorization.require`, a repository, or the compatibility store.

Every response, including cached responses and errors that persist an unknown
state, is returned only after the guard exits successfully. Guard failure during
commit cannot publish the tentative result.

## Prepare, dispatch, commit

| Phase | Durable action | External work |
| --- | --- | --- |
| Prepare | Check current owner/pin/access fingerprint and expiry; reserve request/answer/step hash, random attempt fence, generation and token/call budget atomically | None |
| Dispatch | Read-only use of the committed claim and authorized context | Model and its single bounded repair, outside all guards |
| Commit | New guard, fresh access/catalog checks, same fence and generation CAS, expiry and budget checks, answer/history/result update | None |
| Publication | Occurs after successful guarded commit exit | Return typed compilation |

Preparing authorizes dispatch at that commit point. Revocation after dispatch
can prevent final commit, but does not mean the external model never saw an
already-authorized request. Caller cancellation is propagated to the provider
best-effort; no zero-calls-at-all-instants claim is made.

Context construction captures one immutable `CompilerConfiguration`: provider
identity plus its immutable configuration fingerprint, a copied frozen limits
value, and a tuple of capability forms. That same snapshot supplies the authority
fingerprint, budget reservation, `Dispatch`, and generation, including across
an awaited database claim. Dispatch rejects current configuration drift before
any model call. The real HTTP provider's settings/configuration attributes are
readonly; transport lifecycle state is separate. Repair uses the same captured
provider and limits rather than rereading a mutable compiler.

## State and idempotency

`compiler_clarifications_v1` retains the original payload and gains additive
`guarded_version` and `generation` columns. The new
`compiler_clarification_attempts_v1` table stores immutable step/action hashes and
fences, UTC timestamps, reservations and observed usage. The fence is never
automatically replaced.

| State | Same request / answer | Conflicting request / answer |
| --- | --- | --- |
| No attempt | Claim once under the row lock | Request hash conflict |
| In flight, lease valid | `COMPILATION_IN_PROGRESS`; no new model call | `IDEMPOTENCY_CONFLICT` |
| Lease expired or outcome unknown | `COMPILATION_OUTCOME_UNKNOWN`; no takeover or refund assumption | Conflict |
| Completed | Freshly authorized cached response, no new model call | Conflict |
| Guarded commit rolled back after dispatch | Original in-flight reservation remains; later unknown | Conflict |

Cancellation/crash never clears an in-flight claim. A successful prepare followed
by no observed model completion is uncertain even if a local flag suggests the
request was not sent. There is no 404/local-flag-based retry or automatic
lease takeover. Explicit new work needs a new request ID and its own authorization
and reservation.

Persisted deadlines and leases are DB/UTC timestamps, never serialized
`loop.time()` values. Request processing uses an ephemeral monotonic deadline
clamped to the remaining DB record expiry and token lifetime, covering guard
exit/commit as well as model work. SQL CAS writes independently require
unexpired record and attempt timestamps. Unknown usage stays null.

## Upgrade

`GuardedClarifications.initialize(connection)` performs additive DDL on the
supplied setup transaction. It does not rewrite or delete existing payloads.
Completed legacy rows and their answer histories can be adopted after fresh
authorization/context checks. A legacy `current=null` row has no durable proof
of whether inference began: adoption preserves it and records
`legacy_unknown` with unknown reservation/deadline metadata instead of calling
the model again. The current compatibility store refuses guarded-version rows.
Rollout must drain older binaries before enabling guarded writes; no claim is
made that an already-deployed older binary understands the new fencing columns.

## Evidence boundary

`services/semantic-api/tests/test_guarded_catalog_postgres.py` contains real
PostgreSQL protocol-isolation tests: shared/exclusive gate races, model outside
the guard, two clients, budget reservation, rollback/cancellation, UTC expiry,
stale fence/generation, wrong grants/owner, legacy upgrade, and a loopback HTTP
model response. The fixture deliberately is **not** an OIDC authority.

Actual backend authority, BFF signature/CSRF routing, Keycloak login and public
runtime/result composition are a subsequent integration layer. These isolated
adapter tests do not establish that public product flow by themselves.
