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
- Static deterministic compilation is the only configured provider. There are no credentials,
  live model calls, SQL generation, runtime execution, or data access paths.
- Logs contain request metadata and exception types, not raw questions, catalogs, candidates,
  model payloads, secrets, or exception text.
- Provider calls run in isolated asyncio tasks behind monotonic deadlines; late results are always
  rejected and cancellation is requested. Python cannot forcibly terminate cancellation-resistant
  in-process code, so production adapters must delegate blocking SDK work to a killable worker
  process or use an SDK transport with enforceable network deadlines.

`shared_contract_adapter.py` is the explicit seam for replacing package-internal v0 request and
response models with foundation contracts without changing initializer, compiler, or validator
logic.

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
