# Governed query-runtime plugins

This opt-in package connects server-owned assets to the existing capability planner,
source adapter, DuckDB executor and coordinator. It does not change the offline M0
default, deploy a service, or accept SQL, credentials, paths or connection strings in queries.

The extension protocol is `nexus-plugins/v1`; extended operator payloads explicitly
use `query-runtime/v1`. Legacy `query-runtime/v0` payloads retain their original shape.
Detailed supported forms and evidence live in `contracts/plugins/v1` and the conformance tests.

## Install and run conformance

Install the three workspace packages together (Python 3.12):

```powershell
python -m pip install -c connectors/plugins/requirements-conformance.txt -e services/query-runtime -e connectors/databricks -e "connectors/plugins[dev]"
python -m pytest connectors/plugins/tests -m "not postgres"
```

The local command deliberately **deselects** PostgreSQL tests: it is not database
evidence. `.github/workflows/plugin-conformance.yml` runs the complete suite against
an ephemeral PostgreSQL 16 service and fails if database configuration is absent.
Windows without symlink privilege skips the real symlink test; Linux CI runs it.
The separate M0 release gate continues to run the existing service/UI regressions.
No deployed endpoints, environment auth files or real Databricks credentials are used.

## Integration boundary

| Boundary | Contract and responsibility |
| --- | --- |
| Registration | Server instantiates adapters, immutable descriptors and asset inventories. No dynamic imports, entry-point auto-installation or client registration. |
| Resolver | `GovernedResolver` implements the existing `ParameterizedSourceAdapter`; `AdapterResolver` dispatches the original `SourceResolver` API. |
| Compute | `DuckDBComputePlugin` extends `DuckDBOperatorExecutor`; the existing coordinator calls it, with the same cancellation and memory admission machinery. |
| Results | `ResultStorePlugin` delegates to the existing inline/Parquet `ResultStore`. It adds bounds, not a second manifest/lifecycle implementation. |
| Versioning | Descriptor `nexus-plugins/v1` negotiates accepted runtime versions. New operators require `OperatorSpecV1(version="query-runtime/v1", ...)` and a v1 graph/plan. |
| Compatibility | Source fragments remain the restricted v0 typed interface. Legacy `OperatorSpec` has no added serialized fields and rejects new operator kinds/parameters. |
| Future integration | The generalized semantic compiler can emit the new logical models; durable services can reuse the existing coordinator/store interfaces. Neither integration is claimed here. |

For example, inside an async application, after the server has configured `resolver`:

```python
from nexus_plugins.runtime import (
    DuckDBComputePlugin,
    PluginRuntime,
    ResolverRegistry,
    ResultStorePlugin,
)
from query_runtime.domain import OperatorKind, OperatorSpec, OperatorSpecV1
from query_runtime.planner import ExactConceptBinder, LogicalNode, ValidatedLogicalGraph
from query_runtime.result_store import InlineResultStore

registry = ResolverRegistry([resolver])
graph = ValidatedLogicalGraph(
    version="query-runtime/v1",
    id="synthetic-distinct",
    output_node_id="distinct",
    nodes=(
        LogicalNode(
            id="source", source_alias="orders", operation=OperatorSpec(kind=OperatorKind.SOURCE)
        ),
        LogicalNode(
            id="distinct",
            dependencies=("source",),
            operation=OperatorSpecV1(version="query-runtime/v1", kind=OperatorKind.DISTINCT),
        ),
    ),
)
plan = await registry.plan(graph, ExactConceptBinder({}))
runtime = PluginRuntime(registry, DuckDBComputePlugin(), ResultStorePlugin(InlineResultStore()))
outcome = await runtime.run(plan)
```

`test_runtime_dispatch.py` exercises this path, including two-source set dispatch,
both result stores, full node events, and preflight rejection before source I/O.
This library entry point is intentionally not wired into a public HTTP endpoint or console.

## Configure sources without exposing secrets or paths to queries

All source registrations use `Asset(BoundSource(...), arrow_schema, table_parts)`.
`BoundSource.object_name` is an opaque asset ID, **not** a database relation or filename.
The query's complete bound source must equal a registered source. Discovery returns
only these governed assets; it does not crawl a database or directory.
`schema(asset_id)` executes a zero-row bounded read and checks the source schema.
`connection_test()` checks every configured asset and propagates failures.

| Adapter | Server configuration | Read boundary |
| --- | --- | --- |
| PostgreSQL | `PostgreSQLResolver(assets, CredentialReference(...), provider)`; table parts `(schema, table)` | Psycopg server-side cursor, parameter mapping, read-only transaction, builtin scalar types. |
| Databricks SQL | `DatabricksPlugin(assets, ResolverConfig(...), CredentialReference(...), provider)`; parts `(catalog, schema, table)` | Existing `DatabricksResolver` SQLGlot read-only validator and `StatementExecutionClient`, bounded JSON protocol normalized to Arrow. |
| CSV/Parquet | `GovernedFileResolver(canonical_root, [FileAsset(asset, relative_path)])` | One allowlisted local file per asset; exact schema, bounded sequential batches, no URLs/globs/directory scans. |

The asynchronous `CredentialProvider.resolve(reference)` returns in-memory
server connection settings. PostgreSQL accepts only host/port/dbname/user/password/
sslmode/sslrootcert keys; Databricks accepts only a token. Query/SQG models have
none of these fields. Configure production PostgreSQL TLS verification in the provider;
the adapter does not override an administrator's libpq TLS settings. Never log provider values.

PostgreSQL requires a non-superuser reader without CREATEDB, CREATEROLE, REPLICATION
or BYPASSRLS attributes. It also rejects write privileges on the selected relation
and CREATE on its schema. Use a dedicated role granted USAGE and SELECT only,
not the table/schema owner. The read starts with `default_transaction_read_only=on`,
checks its role inside that transaction, fixes `search_path=pg_catalog`, and applies
statement/lock timeouts. Views, foreign tables, arbitrary functions, user-defined
types, implicit type-changing comparisons and caller SQL are unsupported. Policies
and objects installed by administrators remain trusted; a read-only transaction is
not an OS sandbox and cannot make a malicious database administrator safe.

File roots must be canonical, nonsymlink directories controlled by the server and
not writable by query callers. Absolute/drive/rooted paths, traversal, backslashes,
alternate streams, symlinks/reparse points and unapproved extensions are denied.
Opening checks file identity and rejects content mutation during the scan. This is
not a sandbox against a concurrent privileged administrator replacing root ancestors.
Default file size admission is 64 MiB; CSV blocks are 64 KiB, Parquet batches 512 rows.
Parquet column projection is applied to the reader; filtering occurs in Arrow before
materialization, not by a claim of Parquet row-group statistics pruning.
CSV schema conversion is explicit; strings use Arrow's standard CSV null tokens.

## Operator semantics and resource limits

`contracts/plugins/v1/operator-capabilities.json` accounts for all **26 product
families**, separately from internal SOURCE/LIMIT. It lists accepted forms, parameter
limits and negative cases. The 23 relational/date/reshape families have finite
implemented forms. ASK, ACT and SEARCH are explicitly unsupported: both logical and
physical preflight reject them before any source executes. No tool payload executes.
Registration or an empty result is never evidence for an unsupported capability.

Typed results retain decimal, date, microsecond timestamp and null values. Exact v1
decimal literals use strings with at most 38 digits; floating AVG/division are explicitly
not exact-decimal arithmetic. Set operators require identical ordered Arrow schemas.
JOIN inputs require disjoint column names. UNPIVOT requires homogeneous types.
IMPUTE rejects changes to the input type. DATE/RESAMPLE accept date or naive timestamp;
RESAMPLE aggregates observed buckets only, with no fabricated empty intervals.

Relational outputs are unordered unless a form specifies ordering. SORT ties are
unspecified unless a unique key is supplied. PICK/DEDUPLICATE use row JSON to break
ties deterministically. SAMPLE uses a supplied seed and stable MD5 of typed row JSON;
it is a deterministic fixed-size sample, not a statistically certified sampler.
WINDOW rank/dense_rank retain peer semantics; ROWS aggregates have a bounded
preceding frame, and row-value tie breaking orders peers for ROWS evaluation.

`ResourceLimits` governs row count, retained Arrow buffers, total admitted input
bytes, DuckDB working memory and node time. Output is read in 2,048-row batches,
PostgreSQL in 512-row cursor fetches, with no unbounded prefetch. The shared coordinator
provides concurrency admission/backpressure. Disk spill and DuckDB external access/
extension autoload are disabled; exceeding an engine/buffer budget fails rather than
returning partial success. Limits are not a hard process-RSS sandbox: an individual
native batch/value and database-side work can exceed a Python buffer estimate.
Deploy the process with independent OS/container limits when that guarantee is required.

Cancel addresses an active, unique handle. PostgreSQL sends libpq cancellation;
Databricks uses the existing statement cancel endpoint once a statement ID is known;
files stop between batches; DuckDB interrupts its worker and joins it before closing.
Databricks submission interrupted before the server returns an ID has no known remote
handle to cancel; do not claim exactly-once remote cancellation for that protocol window.

## Evidence and dependency provenance

| Evidence | What it establishes | What it does not establish |
| --- | --- | --- |
| `test_postgres.py`, PostgreSQL 16 CI service | Real type/schema/parameter/role/transaction/limit/cancel behavior | External production PostgreSQL connectivity or SLA |
| `test_databricks.py`, existing Databricks test suite | Existing client/resolver protocol, generated parameter binding, schema/typed results/cancel | Any live Databricks or warehouse execution |
| File/operator/runtime tests | Actual Arrow/Parquet/DuckDB execution, typed edge cases, plugin dispatch and result-store reuse | Durable job recovery, distributed compute or full product rollout |

Direct engine/protocol versions used by conformance are pinned in
`requirements-conformance.txt`; the existing repository uses manifest-based pip
installation, not a universal lockfile. No wholesale platform dependencies were added.

Primary sources used for API/behavior review:

- Psycopg parameter binding: https://www.psycopg.org/psycopg3/docs/basic/params.html
- Psycopg transactions and async connections:
  https://www.psycopg.org/psycopg3/docs/basic/transactions.html and
  https://www.psycopg.org/psycopg3/docs/api/connections.html
- PostgreSQL 16 read-only transaction boundaries:
  https://www.postgresql.org/docs/16/sql-set-transaction.html
- Arrow streaming CSV and scanner projection/filter behavior:
  https://arrow.apache.org/docs/python/generated/pyarrow.csv.open_csv.html and
  https://arrow.apache.org/docs/python/generated/pyarrow.dataset.Scanner.html
- DuckDB MIT license: https://github.com/duckdb/duckdb/blob/v1.5.5/LICENSE

Psycopg 3.3.4 is a separately installed LGPL-3.0-only dependency, not vendored
source. Its binary wheel has its own dependency notices. Redistributors must
retain those licenses/notices and satisfy the LGPL replacement/source obligations.
The application remains Apache-2.0. See the upstream versioned license:
https://github.com/psycopg/psycopg/blob/3.3.4/LICENSE.txt
