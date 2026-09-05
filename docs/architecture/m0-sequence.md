# M0 architecture and governed sequence

## Scope

This document describes the delivered M0 local path. It is intentionally
narrower than the future Azure reference architecture: the semantic backend
hosts the initializer/compiler in-process, the fake resolver is the default,
state is process-local, and live Databricks requires explicit operator opt-in.

## Component boundaries

```mermaid
flowchart TB
    Browser[Vue Web workbench] -->|same-origin HTTP| Edge[Nginx loopback edge]
    Edge -->|/api/v1, Development identity| BFF[ASP.NET Core Control API / BFF]
    BFF -->|bounded typed contract| Backend[Python semantic backend]
    Backend -->|in-process| Init[Initializer]
    Init --> Compiler[Deterministic compiler]
    Compiler --> Validator[SQG schema, graph, ontology, policy validation]
    Validator --> Adapter[Versioned compiler-runtime adapter]
    Adapter --> Planner[Capability-aware physical planner]
    Planner --> Runtime[Query runtime coordinator]
    Runtime -->|default| Fake[Deterministic fake resolver]
    Runtime -. explicit profile .-> Live[Read-only Databricks resolver]
    Runtime --> Local[Local DuckDB operators]
    Fake --> Local
    Live --> Local
    Local --> Store[Inline result store]
    Store --> Commit[Committed result + manifest]
    Runtime --> Events[Run, stage, node events + lineage]
    Commit --> Backend
    Events --> Backend
    Backend --> BFF
    BFF --> Edge
    Edge --> Browser
```

Only the Web/Nginx port is published by root Compose. The Control API and
semantic backend are reachable only on Compose networks. The compiler never
executes SQL and the runtime never accepts a natural-language question.

## Successful run sequence

```mermaid
sequenceDiagram
    actor User
    participant Web
    participant Nginx
    participant BFF as Control API / BFF
    participant Backend as Semantic backend
    participant Compiler as Initializer + compiler
    participant Validator as SQG validator
    participant Runtime as Planner + runtime
    participant Resolver as Fake / live resolver
    participant Store as Result store

    User->>Web: Submit bounded question and options
    Web->>Nginx: POST /api/v1/runs
    Nginx->>BFF: Same-origin request + local identity
    BFF->>BFF: Authenticate, validate, allocate stable run ID
    BFF->>Backend: POST /v1/runs with typed contract
    Backend->>Compiler: Initialize context and compile candidate SQG
    Compiler->>Validator: Validate schema, graph, ontology, policy
    Validator-->>Backend: Normalized validated SQG
    Backend->>Runtime: Adapt and validate physical plan
    Runtime->>Resolver: Execute supported source fragment
    alt deterministic default
        Resolver-->>Runtime: Synthetic tabular source result
    else explicit live Databricks opt-in
        Resolver-->>Runtime: Bounded parameterized read-only result
    end
    Runtime->>Runtime: Execute local-only operators
    Runtime->>Store: Prepare result and manifest
    Store-->>Runtime: Commit acknowledgement
    Runtime-->>Backend: Committed result, manifest, events, lineage
    Backend-->>BFF: Validated status and detail
    loop Until terminal
        Web->>Nginx: GET status/detail
        Nginx->>BFF: Forward
        BFF->>Backend: Refresh typed backend state
        Backend-->>BFF: Monotonic snapshot
        BFF-->>Web: Validated projection
    end
    Web-->>User: Terminal result and provenance
```

The BFF uses `StartPending` and retryable `DispatchUnknown` states to avoid
turning ambiguous transport loss into a false terminal failure. A result is
published only when the result store has committed its manifest. Failed or
cancelled runs do not synthesize a successful manifest.

## Cancellation sequence

```mermaid
sequenceDiagram
    actor User
    participant Web
    participant BFF as Control API / BFF
    participant Backend as Semantic backend
    participant Runtime
    participant Resolver

    User->>Web: Cancel active run
    Web->>BFF: POST /api/v1/runs/{runId}/cancel
    BFF->>BFF: Persist cancellation generation and delivery state
    BFF->>Backend: POST /v1/runs/{runId}/cancel
    Backend->>Runtime: Set cancel event and cancel coordinator
    opt provider statement is active
        Runtime->>Resolver: Request provider cancellation
        Resolver-->>Runtime: Wait for terminal acknowledgement
    end
    Runtime-->>Backend: Terminal Cancelled or safe Failed
    Backend-->>BFF: Terminal snapshot without result/manifest
    BFF-->>Web: Monotonic terminal state
```

Cancellation is a request until terminal acknowledgement. The live adapter does
not report `Cancelled` merely because a provider cancel request was accepted; an
unconfirmed provider outcome becomes a safe failure. A late cancel cannot
overwrite an already committed `Succeeded` result.

## Validation and error boundaries

| Boundary | Enforced behavior | Observable outcome |
| --- | --- | --- |
| Web -> BFF | Request/response shape, identifier, enum, scalar, timestamp, and size validation | Unsafe or malformed payload is rejected before UI state is trusted |
| Nginx local edge | Loopback publication, Host/Origin allowlists, 64 KiB request body, fixed Development identity | Unexpected Host returns 421; non-local Origin returns 403 |
| BFF dispatch | Authentication, authorization, idempotency, per-run coordination, bounded backend timeout | Definitive rejection fails; ambiguous delivery remains `DispatchUnknown` and reconcilable |
| Compiler/SQG | Versioned schema, node order/arity, field flow, ontology membership, policy, and mode topology | Compile/validation diagnostics; runtime is not invoked |
| Adapter/planner | Only a successful normalized SQG is adapted; physical plan and resolver capabilities are validated | Invalid or unsupported plans fail before source execution |
| Resolver | Fake source is deterministic; live SQL is single-query, read-only, parameterized, allowlisted, and bounded | Stable safe diagnostic; no provider payload, SQL, host, rows, or credential text is logged |
| Runtime/local operators | Deadlines, memory/row bounds, cancellation events, and node/stage state transitions | Terminal `Failed`, `TimedOut`, or `Cancelled`; no success-shaped fallback |
| Result commit | Result schema/rows and manifest are validated before atomic in-memory commit | Detail is published only with a committed result identity and checksum |
| BFF detail | Cross-language result, manifest, lineage, and diagnostic limits are revalidated | Invalid backend success payload is rejected rather than persisted |

## Data and trust constraints

- Questions and typed options cross the Web/BFF boundary; credentials do not.
- The BFF owns identity and control-plane metadata but does not compile or
  execute a query.
- The semantic backend owns orchestration and integration adapters but does not
  expose provider credentials or raw provider errors.
- `PIVOT`, `DERIVE`, and `PROJECT` remain local-only in M0. The planner pushes
  down only resolver-declared operations.
- The fake source reads only checked-in synthetic CSVs. Live Databricks uses a
  separate Compose network and is absent from default CI execution.
- Lineage connects logical, physical, source, and committed-result nodes using
  `realized_as`, `reads_from`, `depends_on`, and `produces` relations.

## Related documents

- [HTTP contracts](http-contracts.md)
- [Web HTTP client](web-http-client.md)
- [Local Compose runbook](../operations/local-compose.md)
- [M0 operations, security, and cost runbook](../operations/m0-release.md)
- [M0 release notes](../releases/m0.md)
