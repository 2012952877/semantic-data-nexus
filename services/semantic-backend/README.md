# Semantic backend

The semantic backend owns M0 run orchestration. It invokes the deterministic
`semantic-api` compiler, converts only a succeeded validated SQG through the
versioned source-mapping adapter, plans and executes it with `query-runtime`,
and publishes bounded result and lineage details.

Offline mode is the default and reads the repository's deterministic synthetic
CSV corpus. The service never accepts SQL and does not log request content,
credentials, or provider exception text.

## Run locally

From the repository root with Python 3.12:

```sh
python -m pip install -e services/semantic-api -e services/query-runtime -e "services/semantic-backend[dev]"
semantic-backend
```

The service listens on port `8080` and exposes:

- `GET /health/live`
- `GET /health/ready`
- `POST /v1/runs`
- `GET /v1/runs/{run_id}`
- `POST /v1/runs/{run_id}/cancel`
- `GET /v1/runs/{run_id}/detail`

Run validation with:

```sh
python -m pytest services/semantic-backend
python -m ruff check services/semantic-backend
python -m mypy services/semantic-backend/src
```
