# Local Compose stack

The root Compose topology is the reproducible M0 integration path:

```text
browser -> http://127.0.0.1:8080 -> Web/Nginx -> /api -> Control API -> semantic-backend
```

Only the Web port is published, and it is bound explicitly to
`127.0.0.1`; this development stack is not reachable from other hosts. The
Control API and semantic backend stay on Compose networks, all three containers
run as non-root with read-only root filesystems, and health-gated dependencies
start in backend-to-frontend order. The Web image is always built with
`VITE_NEXUS_CLIENT=http`; this stack never uses the browser mock client.

## Deterministic local stack

Prerequisites are Docker Desktop with Compose v2 and, for the host smoke,
Python 3.12. From the repository root:

```powershell
Copy-Item .env.example .env
docker compose config --quiet
docker compose up --build --wait
python .\scripts\full_stack_smoke.py --base-url http://127.0.0.1:8080
```

The default `fake` resolver reads only checked-in synthetic CSVs.
`SEMANTIC_NEXUS_FAKE_DELAY_MS=750` makes the Execute stage long enough for the
smoke to observe and cancel an active run deterministically. Valid values are
0 through 5000; invalid values fail backend startup.

The local Nginx override injects the fixed, non-secret identity
`local-compose-user` only on proxied `/api` requests. The Control API enables
that header scheme only with `ASPNETCORE_ENVIRONMENT=Development` and already
fails startup if local authentication is enabled in any other environment.
The production-safe config baked into the Web image strips all development
identity headers. Do not remove the loopback-only port binding while this local
identity override is mounted.

To exercise the deployed Vue client in Chromium as well:

```powershell
pnpm --dir apps\web install --frozen-lockfile
pnpm --dir apps\web exec playwright install chromium
$env:NEXUS_FULL_STACK_BASE_URL = "http://127.0.0.1:8080"
pnpm --dir apps\web run test:e2e:full-stack
```

Inspect or stop the stack with:

```powershell
docker compose ps
docker compose logs --no-color --timestamps
docker compose down --volumes --remove-orphans
```

## Live Databricks opt-in

Live execution is not part of CI and cannot be selected through
`compose.yaml` alone. It requires both the `compose.databricks.yaml` override
and the `databricks` profile. The override uses required Compose interpolation,
so configuration fails closed before containers start when host, warehouse, or
token is absent.

Set credentials only in the current shell; do not put them in `.env`, command
arguments, Compose files, image layers, or captured logs:

```powershell
$env:DATABRICKS_WORKSPACE_HOST = "https://your-workspace-host"
$env:DATABRICKS_WAREHOUSE_ID = Read-Host "Warehouse ID"
$secureToken = Read-Host "Databricks token" -AsSecureString
$env:DATABRICKS_TOKEN = [System.Net.NetworkCredential]::new("", $secureToken).Password

docker compose -f compose.yaml -f compose.databricks.yaml --profile databricks up --build --wait

Remove-Item Env:DATABRICKS_WORKSPACE_HOST
Remove-Item Env:DATABRICKS_WAREHOUSE_ID
Remove-Item Env:DATABRICKS_TOKEN
```

Do not run `docker compose config` with live credentials because rendered
Compose output includes environment values. The existing live adapter remains
read-only and parameterized, enforces the reviewed identifier allowlist,
limits output to 1,000 rows and 10 MiB, uses a 30-second statement timeout, and
fails rather than silently truncating or reporting unconfirmed cancellation.
