# Semantic Query Runtime

This clean-room Python 3.12 package executes an already validated, versioned physical plan. It
does not accept natural language, generated SQL, raw LLM output, credentials, or connection
configuration. The core is framework-independent; the included CLI only runs original synthetic
fixtures.

## Quick start

```powershell
cd services\query-runtime
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\pytest
.\.venv\Scripts\ruff check .
.\.venv\Scripts\mypy src
.\.venv\Scripts\python -m build
.\.venv\Scripts\query-runtime complex --result-root .query-runtime-results
```

The `complex` fixture executes a source aggregate followed by local `PIVOT`, `DERIVE`, and
`PROJECT` nodes, commits every successful node as Parquet, and prints the final manifest and safe
lineage graph. `simple` demonstrates a pushed regional quarterly profit aggregate. `join`
demonstrates two concurrent source nodes followed by a local join and sort.

## Boundaries

- `planner.py` accepts a synthetic validated logical graph and an exact `ConceptBinder`. A future
  compiler adapter maps its validated SQG/bound plan into these package-local v0 types.
- `resolver.py` accepts only typed `SourceFragment` objects. `ParameterizedSourceAdapter` is the
  connector seam for an independent Databricks implementation; this package performs no HTTP,
  token, workspace, or SQL-statement handling.
- Local expressions are a closed typed AST. Identifiers are verified against Arrow schemas,
  literals are bound parameters, and division uses `NULLIF(divisor, 0)`.
- `EventStore` and `ResultStore` are replaceable protocols. M0 provides in-memory events with an
  async heartbeat stream plus inline and atomic filesystem Parquet stores. Azure Blob is an
  interface only.

## M0 planning heuristic

Planning is deterministic and capability-aware, not cost-based. Supported
`FILTER`/`AGGREGATE`/`SORT`/`LIMIT` nodes are routed to a source fragment only when the complete
upstream subgraph can be fused for that same source. `PIVOT`, `DERIVE`, `PROJECT`, downstream
operations after a local boundary, and all joins stay local in v0; remote joins require a future
fragment model that names both inputs explicitly. Exact concept bindings rewrite typed operator
references to physical columns and reject cross-source/type mismatches. Node IDs break topological
ties, and each dependency level becomes a wave. M0 intentionally does not estimate cardinality,
reorder joins, choose indexes, or optimize data movement.

## Commit and failure semantics

Filesystem results are written beneath isolated `run_id\node_id\result_id` directories. A Parquet
part, manifest, and `_COMMITTED` marker are created in a temporary sibling directory before one
atomic rename publishes them. Readers reject uncommitted paths, mismatched handles, traversal,
and unbounded pages. A failed, cancelled, or timed-out run never returns a final result handle,
although completed upstream node artifacts and diagnostics remain available for investigation.

Lineage records logical-to-physical realization, source alias/type, operation, dependency,
safe parameter name/type metadata, and result IDs. Parameter values, object payloads, bearer
tokens, connection strings, and arbitrary resolver diagnostics are never recorded.
