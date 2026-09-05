# ADR-0006: Separate product scope, trusted context and delivery evidence

- Status: Accepted for contract foundations; service integration remains planned
- Date: 2026-09-05
- Issues: #29, part of #28
- Extends: ADR-0002 and ADR-0005

## Context

M0 is a released synthetic vertical slice, not a complete standard product.
Observed domain/operator names establish scope but neither reveal private
implementation nor prove behavior in this repository. A single "supported"
boolean would conflate inventory, partial code, integration and verification.
Parallel identity, provider and storage work also needs stable contracts without
silently changing existing v0 consumers.

## Decision

Add `contracts/product/v1` with a versioned capability registry, trusted context,
resource-version identity and append-only audit/usage envelopes. Keep v0 unchanged.
Use existing Draft 2020-12/jsonschema/pytest tooling for structure and deterministic
cross-record validation; do not add a service dependency or authentication layer.

Separate observed-source confidence from `planned`, `implemented`, `integrated`,
`verified` and `unsupported` states. Record M0 gaps explicitly. Require positive,
negative and integration acceptance descriptions for every capability, resolvable
evidence for delivered states and an acyclic dependency graph. An unset acceptance
test reference is a visible gap, never a success.

Trusted context is produced only after verifying OIDC issuer/subject identity and
authorized tenant/workspace membership. Arbitrary headers and schema-valid JSON
are not authority. Resource versions identify immutable scoped bytes. Audit and
usage accept only append envelopes; storage concurrency, immutability, access and
meter reconciliation require separate integration work.

ACT remains unsupported. Future execution must require explicit authorization
of the concrete tool/action/resource; no model, replay or adapter may silently
execute writes. Catalog presence does not authorize a tool.

## Alternatives and consequences

| Alternative | Why not selected |
| --- | --- |
| Add tenant headers to v0 and call the API isolated | Confuses caller input with authority and breaks the existing boundary |
| Mark every visible page/operator supported | Hides missing backend, persistence, authorization and failure behavior |
| Copy the reference product's schemas or prompts | Violates clean-room boundaries and couples us to unowned private semantics |
| Install a broad semantic/agent platform first | Adds unreviewed licensing, authority and adapter assumptions before conformance |
| Treat an append field as durable audit protection | JSON Schema cannot enforce transactions, permissions or storage immutability |

Consumers gain a stable, independently authored boundary and executable scope
checks. The cost is explicit adoption, compatibility review and evidence upkeep.
The registry is not a runtime feature flag or authorization catalog.
The normative field and evidence rules are in
[`contracts/product/README.md`](../../contracts/product/README.md).
