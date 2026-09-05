# M0 operations, security, and cost runbook

## Operating scope

This runbook covers the delivered loopback-only M0 stack and the static Azure
baseline. It does not authorize a production rollout. Root Compose is the
release acceptance topology; live Databricks is an operator-only extension and
is not part of CI.

| Mode | Resolver | Network/data | Intended use |
| --- | --- | --- | --- |
| Root Compose | `fake` | Checked-in synthetic CSVs; backend network is internal | Reproducible acceptance, demos, and troubleshooting |
| Web local mock | Browser mock | No BFF or source connection | UI development and deterministic browser behavior |
| Web local HTTP | BFF HTTP adapter | Approved same-origin/loopback proxy | Contract integration development |
| Compose Databricks profile | `databricks` | Explicit provider HTTPS egress | Controlled read-only operator smoke only |
| Azure Bicep baseline | Deployment-specific | Public baseline unless hardened | Static architecture baseline; not an evidenced deployment |

## Start the deterministic stack

Prerequisites:

- Docker Engine/Desktop with Compose v2;
- Python 3.12 for host smoke scripts;
- port `8080` free on loopback, or an alternate `NEXUS_WEB_PORT`.

From the repository root:

```shell
docker compose config --quiet
docker compose up --build --wait
docker compose ps
python scripts/local_identity_boundary_smoke.py --base-url http://127.0.0.1:8080
python scripts/full_stack_smoke.py --base-url http://127.0.0.1:8080
```

`docker compose up --wait` starts in backend-to-frontend health order. Do not
continue if any service is unhealthy.

To use another loopback port:

```powershell
$env:NEXUS_WEB_PORT = "8081"
docker compose up --build --wait
```

```sh
export NEXUS_WEB_PORT=8081
docker compose up --build --wait
```

## Health and readiness

| Surface | Check | Meaning |
| --- | --- | --- |
| Web/Nginx | `GET http://127.0.0.1:8080/health/live` | Edge process and static server are live |
| Control API | Internal `GET /health/live` | BFF process is live |
| Control API | Internal `GET /health/ready` | BFF can reach semantic-backend readiness |
| Semantic backend | Internal `GET /health/live` | Backend process is live |
| Semantic backend | Internal `GET /health/ready` | Backend resolver/orchestration dependencies are ready |

Only the Web health endpoint is published to the host. Use `docker compose ps`
for internal health rather than publishing additional ports.

## Shutdown and restart

Graceful stop preserves cancellation cleanup time:

```shell
docker compose stop
docker compose down --volumes --remove-orphans
```

The semantic backend has a 60-second stop grace period so active provider
cancellation and resolver cleanup can finish. M0 run state and inline results
are process-local; restart loses them. Do not claim restart recovery or durable
result retention.

For a clean fake-stack recovery:

```shell
docker compose down --volumes --remove-orphans
docker compose build
docker compose up --wait
```

Do not delete repository data or Docker-wide resources as a recovery step.

## Logs and safe diagnostics

Safe local metadata diagnostics:

```shell
docker compose ps --all
docker compose logs --no-color --timestamps
```

The applications are designed not to log questions, SQL, result rows, provider
payloads, credentials, signed URLs, connection strings, or private hosts. Treat
all logs as sensitive until reviewed. Share only the minimum metadata needed
for diagnosis.

Never run or capture `docker compose config` after live credentials are in the
environment: rendered output contains resolved environment values. Never use
shell tracing (`set -x`, `Set-PSDebug -Trace`) around credential setup.

The full-stack workflow captures Compose status, container logs, and Playwright
results only on failure, retains the artifact for seven days, and always runs:

```shell
docker compose down --volumes --remove-orphans
```

## Cancellation

Use the Web workbench cancel action whenever possible. It exercises the full
Web -> BFF -> backend -> runtime path and preserves the stable run ID.

For local API diagnosis through Nginx:

```powershell
$runId = "run_<32-lowercase-hex>"
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8080/api/v1/runs/$runId/cancel"
```

```sh
run_id='run_<32-lowercase-hex>'
curl --fail-with-body --request POST \
  "http://127.0.0.1:8080/api/v1/runs/${run_id}/cancel"
```

`CancelRequested` is non-terminal. Wait for `Cancelled`, `Succeeded`, or
`Failed`. A provider cancel request is not proof of cancellation; the live
adapter waits for terminal acknowledgement and fails safely when it cannot
confirm the outcome. A completed commit remains `Succeeded` if cancellation
arrives late.

## Bounded limits

The following M0 limits are part of the safety boundary:

| Boundary | Limit |
| --- | --- |
| Nginx request body | 64 KiB |
| Question | 4,000 Unicode scalar values |
| BFF semantic-backend timeout | Configurable 1-30 seconds; root Compose uses 15 seconds |
| Inline detail | At most 100 columns and 1,000 rows |
| Live Databricks result | At most 1,000 rows and 10 MiB |
| Live Databricks statement | 30-second connector timeout |
| Fake delay | 0-5,000 ms |
| Result cells | Typed scalar/null only; bounded safe integer, float, and decimal forms |

Do not raise these limits merely to make a failing demo pass. First determine
whether the question is in the supported M0 scope and whether the source/result
contract is valid.

## Development identity and DNS-rebinding boundary

Root Compose mounts a local Nginx configuration that:

- publishes only on `127.0.0.1`;
- accepts Host values only for `127.0.0.1` and `localhost`;
- accepts only empty or local HTTP(S) Origin values;
- injects the fixed non-secret `local-compose-user` identity only after those
  checks;
- forwards identity only to the internal Control API.

The BFF enables this header scheme only in the exact `Development` environment
and independently checks allowed hosts. The production Web image strips
development identity headers. Do not expose the port on `0.0.0.0`, add wildcard
hosts/origins, or enable local authentication in another environment.

## Secret handling

- Keep `.env.example` as names and safe defaults only. Do not commit `.env`.
- Keep tokens out of command arguments, Vite variables, Compose files, image
  layers, fixtures, screenshots, issue text, and logs.
- Supply live values in the current shell or an approved deployment secret
  store, then clear them immediately after use.
- Prefer OAuth/workload identity for automation. A local PAT is a legacy,
  short-lived, least-privilege option.
- In Azure, use separate workload identities, Key Vault references, OIDC for
  deployment automation, and least-privilege data-plane RBAC.
- Run `python scripts/clean_room_scan.py` before review. Findings identify only
  file, line, and rule; the scanner never prints the matched value.

## Live Databricks opt-in

Live execution requires all of the following:

1. Explicit use of both `compose.yaml` and `compose.databricks.yaml`.
2. Explicit activation of the `databricks` profile.
3. A current-shell workspace origin, warehouse ID, and token.
4. Least-privilege `USE CATALOG`, `USE SCHEMA`, `SELECT`, and warehouse
   permissions for synthetic/reviewed objects only.
5. Operator review that the request remains within the 1,000-row, 10 MiB,
   30-second boundary.

Follow the credential-safe commands in
[Local Compose: live Databricks opt-in](local-compose.md#live-databricks-opt-in).
The override fails closed when required values are absent. The release workflow
validates this topology with non-sensitive placeholders but never sends a live
statement.

## Azure baseline caveat

`infra/bicep` defines Container Apps, workload identities/RBAC, ACR, Storage,
Key Vault, PostgreSQL, Search, observability, and optional Azure OpenAI
boundaries. CI builds templates and parameters and runs static regression
assertions.

There is no checked-in evidence that this baseline has been deployed or passed
post-deployment smoke. Do not describe it as a running environment. Before any
deployment:

1. replace placeholder images and commands;
2. use an approved federated deployment identity;
3. run stack-aware what-if and review deletes, RBAC, exposure, and SKU changes;
4. confirm quota, region features, DNS, and PostgreSQL topology;
5. add the production networking/edge controls required by the threat model;
6. deploy development first and collect health, identity, telemetry, and restore
   evidence.

See [Azure deployment operations](azure-deployment.md).

## Cost drivers and controls

| Driver | Safe M0 control |
| --- | --- |
| Container Apps vCPU/GiB and replicas | Scale development to zero; cap replicas; remove abandoned revisions |
| PostgreSQL compute/storage/HA/backups | Use the reviewed development SKU only; set lifecycle ownership before creation |
| Search replicas/partitions | Keep one reviewed development unit; scale only from measured load |
| Log Analytics ingestion/retention | Metadata-only telemetry, sampling, daily cap, short reviewed retention |
| Blob capacity/operations/egress | Synthetic data only; define retention/lifecycle before durable results |
| ACR storage | Basic development SKU; retain only referenced images |
| Databricks SQL warehouse | Auto-stop, bounded statements, least-privilege synthetic data, no unattended live smoke |
| Optional Azure OpenAI | Disabled by default; quotas, budgets, and workload token limits before enablement |
| Private networking and edge services | Required by production threat model; avoid idle development instances |

Exact prices depend on region, agreement, and date. Use current Azure pricing,
budgets, anomaly alerts, and required `environment`, `workload`, `owner`, and
`costCenter` tags before approval.

## Troubleshooting

| Symptom | Safe action |
| --- | --- |
| `docker compose config` fails in fake mode | Clear live-only environment variables and validate plain `compose.yaml` |
| Live profile reports a missing value | Set it in the current shell; do not weaken required interpolation |
| Service remains unhealthy | Inspect `docker compose ps --all` and metadata-only logs; rebuild that stack |
| Browser returns 421 | Use `127.0.0.1` or `localhost`; do not add a wildcard Host |
| Browser/API returns 403 for Origin | Use the same local origin; do not disable the Origin gate |
| Run stays `DispatchUnknown` | Retry status/reconciliation with the same run/client request identity |
| Run is `Failed` during compile | Inspect stable diagnostics; do not bypass SQG/ontology validation |
| Live result exceeds a bound | Narrow the governed question/source; do not silently truncate |
| Cancellation is not terminal | Wait for backend/provider acknowledgement; preserve evidence and stop safely |
| Restart lost run history | Expected in M0; durable repository/result adapters are future work |

Do not troubleshoot by printing environment variables, provider payloads,
questions, SQL, rows, or credentials.

## Release verification

The [M0 release gate](../../.github/workflows/m0-release-gate.yml) calls the
existing contract, evaluator, Python, Web, .NET, Bicep, and full-stack workflows
at the same commit, then aggregates them into one terminal check. It runs on
every pull request, merge queue event, main push, and manual dispatch so
cross-component changes cannot evade the fan-in gate.

Local hosts without Docker, .NET, or Azure CLI can still run the tracked-content
scan, Python package gates, Web gates, and deterministic fixture checks. Missing
platform gates must remain visible as a local limitation and be completed by
GitHub Actions; they must not be reported as locally passed.
