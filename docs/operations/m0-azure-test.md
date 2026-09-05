# M0 authenticated Azure test

Use this path to try the real M0 application over ordinary HTTPS without
deploying the future PostgreSQL/Search/model baseline. The temporary login is
**HTTP Basic authentication, not Microsoft Entra sign-in**. It protects a
shared synthetic test identity; it is not production or per-user authorization.

Contents:

1. Understand the boundary
2. Provision the isolated test
3. Verify access and execution
4. Try the supported scenarios
5. Control costs and remove the environment

The route is: isolated foundation -> reviewed images -> private credentials ->
authenticated revision -> real API and browser exercises. Commands below are
PowerShell unless marked otherwise. Keep real deployment parameters and
credentials outside Git.

## 1. Understand the boundary

The public URL terminates TLS at Azure Container Apps (ACA), Azure's managed
container hosting service. One Consumption replica runs the same three
production-built images as local Compose.

| Component | Binding | Boundary |
| --- | --- | --- |
| Nginx / Vue | 8080, the only ingress target | Basic authentication for every UI and API route |
| BFF / ASP.NET | `127.0.0.1:8081` | Real BFF; fixed synthetic Development identity only from Nginx |
| Python backend | `127.0.0.1:8082` | Real initializer, compiler, runtime, and synthetic resolver |
| Nginx health server | 8083, no external port mapping | Readiness calls BFF readiness, which checks the backend |
| Private registry | ACR Basic | Admin/anonymous access disabled; managed identity has registry-scoped `AcrPull` |

Nginx requires the exact application Host and same-origin API requests,
discards upstream `Authorization`, and overwrites `X-Dev-Subject`,
`X-Dev-Name`, and `X-Dev-Roles`. The Basic password never reaches the BFF.
The password hash and Nginx configuration are mounted as secret volumes.
Never expose the root `compose.yaml` Development entry directly.

The environment has no VNet, NAT, Dedicated profile, log workspace, database,
search service, or model deployment. The backend uses checked-in synthetic
data, not live Databricks. The image's Uvicorn entry point is overridden to
bind the real backend to loopback; this is not a mock HTTP service.

These boundaries are defined in `infra/bicep/m0-aca*.bicep` and
`infra/bicep/config/nginx.m0-aca.conf`. Azure probe and storage behavior:
`https://learn.microsoft.com/azure/container-apps/health-probes` and
`https://learn.microsoft.com/azure/container-apps/storage-mounts`.

## 2. Provision the isolated test

Prepare the following before creating resources. Use existing Azure sign-in;
do not copy a password, client secret, or GitHub token into deployment code.

| Required item | Purpose |
| --- | --- |
| Azure CLI with Bicep and Container Apps commands | Compile and deploy |
| Resource creation plus role-assignment permission | Create the foundation and scoped registry pull assignment |
| One new, dedicated resource group | Avoid changes to existing workloads |
| Python 3.12 and OpenSSL on the operator machine | Smoke tests and salted password hashing |
| Globally unique registry name | Private image builds |
| New owner-only credential directory | Store only this temporary test login |

Set non-secret variables in the current shell, substituting approved values:

```powershell
$subscription = $env:AZURE_SUBSCRIPTION_ID
$group = '<new-test-resource-group>'
$location = 'eastus2'
$registry = '<unique-alphanumeric-registry-name>'
$environment = 'sdn-test-environment'
$identity = 'sdn-test-registry-pull'
$app = 'sdn-test-https'
$imageTag = '<reviewed-source-tag>'
if (-not $subscription) { throw 'Set the intended subscription explicitly.' }

az group create --subscription $subscription --name $group --location $location
az deployment group create --subscription $subscription --resource-group $group `
  --name m0-aca-foundation --template-file .\infra\bicep\m0-aca-foundation.bicep `
  --parameters registryName=$registry environmentName=$environment pullIdentityName=$identity
```

The foundation must succeed before image builds. It creates no running Nexus
revision. The omitted log destination must remain null; the literal string
`none` is not an accepted ARM value.

Create and inspect a reviewed archive, excluding even environment example
files. Use a new temporary directory for extraction:

```powershell
$source = Join-Path $env:TEMP ([guid]::NewGuid().ToString())
$archive = "$source.tar.gz"
git archive --format=tar.gz --output=$archive HEAD `
  .dockerignore compose.yaml apps connectors contracts data ops packages `
  pyproject.toml scripts services ':(exclude)**/.env*'
tar -tf $archive
New-Item -ItemType Directory -Path $source | Out-Null
tar -xzf $archive -C $source

Push-Location $source
az acr build --subscription $subscription --registry $registry `
  --image "semantic-backend:$imageTag" --file services\semantic-backend\Dockerfile .
Push-Location services\control-api
az acr build --subscription $subscription --registry $registry `
  --image "control-api:$imageTag" --file Dockerfile .
Pop-Location
Push-Location apps\web
az acr build --subscription $subscription --registry $registry `
  --image "web:$imageTag" --file Dockerfile .
Pop-Location
Pop-Location
```

Check each native command's exit status before continuing. The archive must
contain no `.git`, local `.env`, private keys, or operator credentials.
Registry tasks build private source in Azure; do not upload it elsewhere.

For a new environment, generate a fresh login in a **new dedicated directory**.
For an existing environment, reuse its authorized credential file instead of
silently rotating it:

```powershell
$credentialDirectory = Join-Path $HOME ".ssh\$app-login"
if (Test-Path -LiteralPath $credentialDirectory) { throw 'Refusing to overwrite an existing login.' }
New-Item -ItemType Directory -Path $credentialDirectory | Out-Null
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
  $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
Set-Acl -LiteralPath $credentialDirectory -AclObject $acl
$generatedLoginValue = [Convert]::ToBase64String(
  [Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
$hash = $generatedLoginValue | openssl passwd -6 -stdin
if ($LASTEXITCODE -ne 0) { throw 'Password hashing failed.' }
$credentialFile = Join-Path $credentialDirectory 'login.json'
$domain = az containerapp env show --subscription $subscription `
  --resource-group $group --name $environment --query properties.defaultDomain -o tsv
@{
  username = 'm0-tester'
  password = $generatedLoginValue
  htpasswd = "m0-tester:$hash`n"
  url = "https://$app.$domain"
} | ConvertTo-Json | Set-Content -LiteralPath $credentialFile -Encoding utf8
Remove-Variable generatedLoginValue, hash
```

Do not print the credential object, put credentials in a URL, or pass the
password as a command-line argument. OpenSSL receives it on stdin. Only the
salted hash enters ACA. Nginx's password-file format is documented at
`https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html`.

Deploy the first application revision with authentication already present:

```powershell
$login = Get-Content -LiteralPath $credentialFile -Raw | ConvertFrom-Json
$parametersFile = Join-Path $credentialDirectory 'runtime.parameters.json'
@{
  '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
  contentVersion = '1.0.0.0'
  parameters = @{
    registryName = @{ value = $registry }
    environmentName = @{ value = $environment }
    pullIdentityName = @{ value = $identity }
    appName = @{ value = $app }
    imageTag = @{ value = $imageTag }
    revisionSuffix = @{ value = "test-$([guid]::NewGuid().ToString('N').Substring(0,8))" }
    fakeDelayMs = @{ value = 5000 }
    authFile = @{ value = $login.htpasswd }
  }
} | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $parametersFile -Encoding utf8
try {
  az deployment group create --subscription $subscription --resource-group $group `
    --name m0-aca-app --template-file .\infra\bicep\m0-aca.bicep `
    --parameters "@$parametersFile" --query properties.outputs.appUrl.value -o tsv
  if ($LASTEXITCODE -ne 0) { throw 'Authenticated revision deployment failed.' }
} finally {
  Remove-Item -LiteralPath $parametersFile -Force
}
```

The explicit test delay is bounded to 5,000 ms by the existing synthetic
resolver. It makes active cancellation observable over a higher-latency HTTPS
connection; it does not substitute a mock BFF or runtime. The template's
unchanged default is 750 ms. Use a new revision suffix for mounted
configuration changes. Keep the same password unless rotation is intentional.

## 3. Verify access and execution

An ARM deployment reporting `Succeeded` is not sufficient. Check current
replicas and real HTTP responses before calling the URL usable.

| Check | Required evidence |
| --- | --- |
| Current replica | All three containers ready/running, no repeating restarts |
| Anonymous UI and API | HTTP 401 with a Basic challenge; no workbench or principal |
| Wrong password | HTTP 401 |
| Correct password | UI loads and `/api/v1/me` returns HTTP 200 |
| Spoofed `X-Dev-*` | Cannot authenticate anonymously or replace the fixed identity |
| Foreign Origin | API rejected with HTTP 403 |
| Health port | Ingress target 8080; no `additionalPortMappings` for 8083 |
| Real semantic path | Simple, complex, committed manifest/lineage, and active cancellation |

Inspect the current revision and replicas with `az containerapp show` and
`az containerapp replica list`. A historical `latestReadyRevisionName` can
coexist with a current CrashLoop; do not use that field alone as proof.
Nginx's map/server-name buckets are explicitly sized for long ACA hostnames.
An error such as `could not build map_hash` is a real startup failure, not an
authentication success.

Run the existing API smoke through the same public HTTPS URL. Credentials are
read locally and passed only in the test process environment:

```powershell
$login = Get-Content -LiteralPath $credentialFile -Raw | ConvertFrom-Json
$env:NEXUS_SMOKE_USERNAME = $login.username
$storedValue = $login.password
$env:NEXUS_SMOKE_PASSWORD = $storedValue
try {
  python scripts\full_stack_smoke.py --base-url $login.url --timeout 60
} finally {
  Remove-Item Env:\NEXUS_SMOKE_USERNAME, Env:\NEXUS_SMOKE_PASSWORD
  Remove-Variable storedValue
}
```

The authenticated smoke refuses plain HTTP and redirects. It checks actual
2024 fixture values and requires cancellation to reach `Cancelled` with no
published result or manifest. A run that already succeeded is not a
successful cancellation.

The existing Web browser smoke accepts `NEXUS_FULL_STACK_BASE_URL`,
`NEXUS_TEST_USERNAME`, and `NEXUS_TEST_PASSWORD`. Its authenticated mode disables
trace capture to avoid storing credentials. Run `pnpm run test:e2e:full-stack`
from `apps\web` after restoring the existing dependencies. The test's fixed
2024 clock is a deterministic test fixture, not the real browser's default.

Deployment evidence from 2026-09-05: the real authenticated HTTPS path passed
the fixed-clock simple, complex, and active-cancellation smoke; the existing
deployed-browser test also passed. An independent browser exercise using the
real current clock returned 20 rows for four regions and displayed the result,
lineage, and SHA-256 manifest. Environment-specific URLs, run IDs, and login
material remain in the operator's private handoff, not in Git.

## 4. Try the supported scenarios

Open the returned HTTPS URL with `/ask` and use the username/password from the
owner-only file. No SSH tunnel is required for this ACA entry.

| Scenario | How to exercise it |
| --- | --- |
| Manual browser, real current date | Type `按区域和季度汇总利润` |
| Regional previous-quarter fixture | API mode `regional_quarterly_profit`, question `上季度各区域利润是多少?`, evaluation clock `2024-04-15T09:00:00Z` |
| Monthly comparison fixture | API mode `monthly_regional_comparison`, question `对比各区域销售利润, 按月份`, same fixed clock |
| Cancellation | Cancel while Execute is active, before the result commits |

The manual no-relative-time question returns 20 rows across four synthetic
regions in the current fixture set. The browser does not expose an evaluation
clock or compilation-mode selector. The monthly comparison therefore needs
the API or the smoke script; do not claim arbitrary natural-language support.
The old sales-target/year example chips are not supported M0 demonstrations.

For both successful paths, open the run detail and inspect the committed
result, typed stages/physical plan, manifest checksum, and lineage. The complex
path must include `AGGREGATE -> PIVOT -> DERIVE -> PROJECT`.

## 5. Control costs and remove the environment

The app is **min 0 / max 1**, with 1 vCPU and 2 GiB total. Scale-to-zero and
revision/container restarts erase both BFF and backend in-memory history.
Saved run IDs are not durable links; recreate the fixtures when needed.

| Meter | Retail basis checked 2026-09-05, USD |
| --- | --- |
| ACA active compute, 1 vCPU + 2 GiB | Approximately $2.59 for 24 continuously active hours, before free allowances |
| ACA scaled to zero | No running compute charge |
| ACR Basic | $0.1666/day, plus any applicable storage overage |
| ACR Tasks | $0.0001/vCPU-second outside applicable free usage |
| HTTP requests | $0.40/million outside applicable free usage |
| Optional retained D4as_v6 VM | $0.182/hour; approximately $4.65/day including a 64-GiB standard SSD and one standard IPv4 |

Rates are estimates, not an invoice. Ingestion workspaces are not created;
egress, disk operations, discounts, taxes, and shared free allowances are not
included. First-party sources:
`https://prices.azure.com/api/retail/prices` and
`https://learn.microsoft.com/azure/container-apps/billing`.

The optional `m0-private-vm.bicep` is separate from ACA and is not required for
the public HTTPS entry. A user-requested retained VM continues billing even
when ACA scales to zero. Do not stop or resize it without approval.
Deallocating a VM does not remove disk, public-IP, or registry charges.

To remove only ACA, explicitly delete the test container app, its managed
environment, registry, and dedicated pull identity/role assignment; preserve
any retained VM. To remove the **entire** isolated test, including the VM,
obtain approval and delete the dedicated resource group:

```powershell
az group delete --subscription $subscription --name $group --yes
```

After the service is removed, delete its temporary login file. Preserve user
SSH keys and trust material unless their deletion was separately requested.
Do not change repository visibility or move the `v0.1.0` tag.
