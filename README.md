# Semantic Data Nexus

Semantic Data Nexus turns a supported business question into a checked query plan, executes that plan, and shows the result together with its calculation steps and data lineage.

**Current delivery: a network-accessible core preview, not the complete commercial product.** The hosted preview uses a real Azure OpenAI model and real application/runtime code. Its business data and shared test identity are synthetic.

| Delivery fact | Current value |
| --- | --- |
| Hosted entry | [Open the protected Ask page](https://sdn-m0-https.salmonbush-f52c1293.eastus2.azurecontainerapps.io/ask) |
| Application image source | `b4b5f0c918b1b2899b5c69029814b551b568789c` |
| Observed model | `gpt-4.1-mini-2025-04-14` |
| Browser demonstration | 2026-09-06; 20 committed region/quarter result rows |
| Authentication | HTTPS Basic authentication in front of one shared synthetic application identity |
| Persistent run history | Not provided by this preview; restarting the application can clear history |
| Hosted CI | Not re-established: the repository owner's GitHub Actions allowance is exhausted |

The source reference above identifies the application images. The deployment recipe in this branch additionally handles Azure API readback fields, identity environment aliases, and Linux startup line endings. The original `v0.1.0` tag and other development branches are not replaced by this preview.

## 1. Try the online flow

Obtain the existing demo login from the operator through a private channel. Credentials are not published in this repository. This browser login is **not Microsoft Entra user sign-in**, even though the application itself uses a managed identity to call Azure.

1. Open the protected Ask page and authenticate.
2. Click the single supported example. It asks for profit summarized by region and quarter.
3. Start the run. Observe Initialize, Compile, Optimize, Execute, and Generate.
4. Wait for the committed result, rather than treating an accepted request as success.
5. Check the model name returned in the server's run record.
6. Open the run inspection link to see the node plan, source lineage, and committed result manifest.

The browser demonstration returned **20 rows**, not 20 distinct regions. The data are checked-in fixtures, not live company sales.

The [captured evidence record](docs/assets/network-demo/network-demo-evidence.json) records the run ID, model, token counts, data classification, and image-source reference. In that browser run, the server reported 3,010 input tokens and 292 output tokens. These are observations for that run, not a pricing or throughput guarantee.

Actual browser screenshots are delivered separately to the operator: this repository's existing clean-room rules prohibit tracked raster screenshots. The portable evidence record is retained in Git.

Run-detail URLs are temporary because history is currently process-local. Saved evidence documents that earlier run; it must not be presented as a new live run.

### Supported and unsupported questions

| Path | Supported use |
| --- | --- |
| Current Ask page | Region/quarter profit aggregation using the supplied example |
| Typed API regression | Regional previous-quarter profit with an explicit historical evaluation clock |
| Typed API regression | Monthly regional comparison, including pivot and derived profit change |
| Active cancellation | Cancel an executing run before it commits results |

The UI does not expose the API's historical evaluation-clock or compilation-mode selectors. Its example deliberately avoids a relative date such as "last quarter": fixture periods are not continuously regenerated to match the current year.

Sales targets, target-attainment rates, arbitrary year-on-year comparisons, arbitrary uploads, and unrestricted natural-language questions are **not** supported merely because a language model is connected. The earlier UI suggested unsupported questions; those suggestions have been removed from HTTP mode.

## 2. What this version delivers

| Capability | Status in this delivery | Boundary |
| --- | --- | --- |
| Protected public UI and API | Deployed and exercised | Shared synthetic identity, not production user isolation |
| Real Azure model compilation | Exercised through the public API and browser | Bounded, server-selected model and approved semantic scope |
| Quarter-profit results | Exercised against exact synthetic fixture values | Limited governed scenario |
| Monthly comparison | Exercised through the typed API | Not a separate free-form UI mode |
| Result table, typed stages, lineage, checksum | Exercised through the public site | Only committed successful results are published |
| Cancellation | Observed Running to Cancelled, with no result or manifest | Cancellation does not refund model work already performed |
| Ontology editing, knowledge bases, durable storage, configuration and commercial administration | Further work exists in other development branches | Not all included, integrated, or deployed by this branch |
| Production identity, backup/restore, HA, load qualification and full commercial assurance | Outside this acceptance | No production-readiness or certification claim |

One live attempt was rejected with `PROVIDER_SCHEMA` before execution because the generated graph did not satisfy the local schema. A later cancellation attempt completed successfully. The validation boundary was not disabled, and rejected output was not replaced with a canned result. This is a preview with observable failure states, not a claim that every generation succeeds.

## 3. How the system works

A **semantic model**, also called an ontology here, defines the business terms, fields, metrics and relationships that the application is allowed to use. It prevents a model from inventing an arbitrary database structure.

A **Semantic Query Graph (SQG)** is a structured description of operations such as selecting fields, filtering, grouping, aggregating, sorting and projecting an output. It is not executable model-generated SQL.

The request follows this sequence:

1. The browser submits a bounded question and typed execution options.
2. The **BFF** (backend for frontend) authenticates the application request, maintains its run state, and forwards it to the Python backend.
3. The initializer resolves the permitted semantic context. Azure OpenAI proposes a graph within that context.
4. Schema and semantic validators reject malformed graphs, unknown concepts, invalid operators, and out-of-scope references.
5. The runtime plans and performs the allowed operations over the configured source.
6. A successful run publishes a result and a **manifest**: a record of the committed result, including its schema, row count and checksum.

| Component | Implementation | Responsibility in the hosted preview |
| --- | --- | --- |
| Browser workbench | Vue 3 and TypeScript | Ask, progress, results and run inspection |
| HTTPS entry | Azure Container Apps and Nginx | TLS, Basic authentication, Host/Origin checks, same-origin routing |
| Control API / BFF | ASP.NET Core 8 | Typed requests, identity boundary, run lifecycle and response validation |
| Semantic backend | Python 3.12 and FastAPI | Orchestration and compiler/runtime integration |
| Initializer and compiler | `semantic-api` package, in the backend process | Semantic selection, structured model call and graph validation |
| Query runtime | DuckDB, PyArrow and bounded operators | Actual data processing and committed result publication |
| Model | Existing Azure OpenAI deployment | Propose a graph; never receive authority to run arbitrary SQL or tools |
| Source | Repository-generated CSV fixtures | Synthetic business data |

There are three application containers in the hosted deployment: Web/Nginx, BFF, and Python backend. The initializer/compiler is not a fourth public service.

### Three modes that must not be confused

| Browser client | Server compiler | What is real |
| --- | --- | --- |
| `mock` | No BFF required | Browser fixtures only; suitable for UI development |
| `http` | `static` | Real BFF/backend/runtime with deterministic offline compiler fixtures |
| `http` | `azure_openai` | Real BFF/backend/runtime and a real Azure model call |

The hosted preview uses the third row. Its resolver setting remains `fake` because that resolver reads controlled synthetic data. **A synthetic data source is not the same thing as a mocked model response.**

The server selects the model. A browser label or request field cannot silently switch a configured Azure provider back to static mode.

## 4. Repository guide

| Directory | Start here for |
| --- | --- |
| `apps\web` | UI, validated HTTP client, unit tests and browser tests |
| `services\control-api` | BFF, authentication, run state and typed backend contracts |
| `services\semantic-backend` | Orchestration, response models, source mapping and integration tests |
| `services\semantic-api` | Initializer, compiler, Azure provider and strict credential handling |
| `services\query-runtime` | Planning, operators, execution and result handling |
| `connectors\plugins` | Versioned connector/operator boundaries and conformance |
| `connectors\databricks` | Separate opt-in read-only Databricks integration |
| `contracts` | Language-neutral schemas and compiler contracts |
| `data\synthetic` | Fixture generators and generated data |
| `evals` | Deterministic reference evaluation |
| `infra` | Infrastructure templates and guarded deployment helpers |
| `scripts` | Full-stack smoke and clean-room scanning |
| `docs` | Architecture, runbooks, release background and evidence |

Useful deeper references:

- [Web guide](apps/web/README.md)
- [HTTP client contract](docs/architecture/web-http-client.md)
- [Architecture and sequence](docs/architecture/m0-sequence.md)
- [Local Compose runbook](docs/operations/local-compose.md)
- [Authenticated Azure baseline](docs/operations/m0-azure-test.md)
- [Roadmap](docs/roadmap.md)

Older documents describe their original M0 or component scope. They are not evidence that every later feature is enabled in this hosted preview.

## 5. Run locally

### Prerequisites

| Task | Required tools |
| --- | --- |
| Full local Compose baseline | Docker Engine/Desktop with Compose v2; Python 3.12 for the smoke script |
| Python development | Python 3.12 and pip |
| Web development | Node.js 22 and pnpm 10.33.2 |
| BFF development | .NET SDK 8; installing only the runtime is insufficient |
| Azure operations | Azure CLI, access to the approved subscription/resources, and explicit operation approval |

The repository is private. Cloning requires an authorized GitHub identity.

```powershell
git clone --branch 2012952877-network-demo-20260906 https://github.com/2012952877/semantic-data-nexus.git
Set-Location semantic-data-nexus
```

### Full offline stack

From the repository root:

```powershell
docker compose config --quiet
docker compose up --build --wait
python scripts\full_stack_smoke.py --base-url http://127.0.0.1:8080
```

Open `http://127.0.0.1:8080/ask`. Root Compose binds the public port to loopback and uses the offline static compiler. It does not reproduce the hosted Azure-model mode or automatically spend model tokens.

These are the repository's existing Compose instructions. This delivery was exercised through Azure-built images and the public HTTPS entry; a new local Docker full-stack run was not performed during this handoff.

Stop this local Compose project without implicitly deleting volumes:

```powershell
docker compose down
```

Do not expose root Compose directly to the Internet. The hosted shared-identity demo has additional authenticated ingress protection.

### Python development without Docker

The following installs into a project-local environment, not the global Python installation:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e evals -e connectors\databricks -e connectors\plugins -e "services\semantic-api[dev,postgres]" -e services\query-runtime -e "services\semantic-backend[databricks,dev,eval]" build

$env:SEMANTIC_NEXUS_AUTH_MODE = 'legacy-development'
$env:SEMANTIC_NEXUS_ENVIRONMENT = 'Development'
.\.venv\Scripts\python.exe -m semantic_backend.demo --scenario simple
.\.venv\Scripts\python.exe -m semantic_backend.demo --scenario complex
```

These two commands were exercised with PyArrow 23.0.1. They are offline execution demonstrations, not proof of a live model connection.

For browser-only development:

```powershell
pnpm --dir apps\web install --frozen-lockfile
pnpm --dir apps\web dev
```

The standalone Web development default is `mock`. Use the documented HTTP/Compose path when checking the real BFF and backend.

## 6. Configuration and security

| Setting | Meaning / hosted value |
| --- | --- |
| `VITE_NEXUS_CLIENT` | `http` in the hosted image; a build-time browser setting |
| `SEMANTIC_COMPILER_MODE` | `azure_openai` for the hosted preview; `static` for offline operation |
| `SEMANTIC_COMPILER_ENDPOINT` | The explicitly approved Azure origin plus `/openai/v1/chat/completions` |
| `SEMANTIC_COMPILER_MODEL` | The expected model version, currently `gpt-4.1-mini-2025-04-14` |
| `SEMANTIC_COMPILER_AZURE_DEPLOYMENT` | The actual deployment name, distinct from the model-version identifier |
| `SEMANTIC_COMPILER_AZURE_AUTH` | `managed_identity` in Azure; no model API key is embedded |
| `SEMANTIC_COMPILER_AZURE_TENANT_ID` | The explicitly approved tenant |
| `SEMANTIC_COMPILER_AZURE_CLIENT_ID` | The assigned identity's client ID, not its principal/object ID |
| `SEMANTIC_COMPILER_AZURE_MANAGED_IDENTITY_SOURCE` | `app_service` for this Container Apps deployment |
| `SEMANTIC_COMPILER_AZURE_MANAGED_IDENTITY_ENDPOINT` | The platform endpoint captured by the guarded startup command; not a user-supplied URL |
| `SEMANTIC_COMPILER_MAX_INPUT_TOKENS` | 32,768 |
| `SEMANTIC_COMPILER_MAX_OUTPUT_TOKENS` | 2,048 |
| `SEMANTIC_COMPILER_MAX_CONCURRENT_CALLS` | 1 |
| `SEMANTIC_COMPILER_TIMEOUT_SECONDS` | 20; the orchestration deadline is separate |
| `SEMANTIC_NEXUS_RESOLVER` | `fake`, the controlled synthetic-data resolver |

Identity values must come from the approved environment, not from a user question or a committed credential file.

Browser `VITE_*` variables are public after a build. Never put passwords, API keys, access tokens, or client secrets in them.

The hosted boundary deliberately remains a **shared synthetic test**:

- Basic authentication protects every UI and API route.
- Nginx validates the Host and Origin, strips incoming authorization before forwarding, and overwrites the synthetic identity headers.
- BFF and backend listen on loopback inside the container app; the separate health port is not publicly mapped.
- The existing application identity retains its registry pull permission and has only an account-scoped model-use grant for this integration. No subscription-wide Owner/Contributor grant is required.
- The application-to-Azure managed identity is separate from browser-user authentication. It does not turn the shared browser login into production SSO.
- Validators remain enabled. Unknown concepts, invalid graphs, unsupported operators and malformed responses must fail rather than become plausible-looking results.

Production per-user authentication, workspace isolation, durable storage and operational assurance require the corresponding product integration; do not reuse this test-only identity design for customer data.

## 7. Build, update and roll back the existing Azure demo

This is an **upgrade recipe for the already approved three-container application**, not permission to create arbitrary resources. The current profile preserves one always-on replica with 1 vCPU and 2 GiB total.

### Prepare a fixed image source

First review and commit the application changes. Build from a fresh `git archive`, not a dirty working directory containing local environments or credentials.

```powershell
$sourceCommit = (git rev-parse HEAD).Trim()
$tag = "network-" + $sourceCommit.Substring(0, 12)
$archive = Join-Path $env:TEMP "$tag.tar"
$context = Join-Path $env:TEMP $tag
if ((Test-Path $archive) -or (Test-Path $context)) {
    throw 'Use a new export location; do not overwrite recorded build inputs.'
}
git archive --format=tar --output=$archive $sourceCommit .dockerignore README.md apps connectors contracts data infra ops packages pyproject.toml scripts services ':(exclude)**/.env*'
if ($LASTEXITCODE -ne 0) { throw 'Source export failed.' }
New-Item -ItemType Directory -Path $context | Out-Null
tar -xf $archive -C $context
if ($LASTEXITCODE -ne 0) { throw 'Source extraction failed.' }
Get-FileHash $archive -Algorithm SHA256
```

Set the approved subscription and existing registry explicitly. Do not change the Azure CLI's global default account as part of deployment.

```powershell
$subscription = $env:AZURE_SUBSCRIPTION_ID
$registry = $env:NEXUS_REGISTRY_NAME
if (-not $subscription -or -not $registry) { throw 'Approved resource settings are required.' }

Push-Location $context
az acr build --subscription $subscription --registry $registry --image "semantic-backend:$tag" --file services\semantic-backend\Dockerfile --platform linux --timeout 1800 --no-wait .
Pop-Location
```

Record that run ID and wait for it to succeed before building the next image. Use `services\control-api` as the BFF build context and `apps\web` as the Web context; both use their own `Dockerfile`.

```powershell
az acr task list-runs --subscription $subscription --registry $registry --output table
az acr task show-run --subscription $subscription --registry $registry --run-id <recorded-run-id>
```

`--no-wait` can return without a JSON result. An empty CLI result is **not** a reason to submit the same build again. Identify the recorded run, verify its image tag and source, and inspect `agentConfiguration.cpu`. This handoff used three sequential 2-vCPU quick runs, one per image.

Collect each successful run's immutable output digest into an operator-owned JSON manifest:

```json
{
  "sourceCommit": "<40-character source commit>",
  "images": {
    "web": "<registry>.azurecr.io/web@sha256:<digest>",
    "control-api": "<registry>.azurecr.io/control-api@sha256:<digest>",
    "semantic-backend": "<registry>.azurecr.io/semantic-backend@sha256:<digest>"
  }
}
```

The placeholders must be replaced with observed values. Do not overwrite old image tags or use `latest` as the rollback reference.

### Generate a template-only patch

Capture the current app's identity, revision, registry references, ingress and template without exporting secret values:

```powershell
$group = $env:NEXUS_RESOURCE_GROUP
$app = $env:NEXUS_APP_NAME
$operatorDirectory = Join-Path $env:LOCALAPPDATA 'SemanticDataNexus\deployments'
New-Item -ItemType Directory -Path $operatorDirectory -Force | Out-Null
$snapshotPath = Join-Path $operatorDirectory "$tag-app-before.json"
if (Test-Path $snapshotPath) { throw 'Do not overwrite the rollback snapshot.' }
az containerapp show --subscription $subscription --resource-group $group --name $app --query '{id:id,location:location,identity:identity,properties:{latestRevisionName:properties.latestRevisionName,configuration:{activeRevisionsMode:properties.configuration.activeRevisionsMode,ingress:properties.configuration.ingress,registries:properties.configuration.registries},template:properties.template}}' --output json > $snapshotPath
if ($LASTEXITCODE -ne 0) { throw 'Snapshot failed.' }
```

Keep operator snapshots and generated patches outside Git. Fill the following parameters from the approved deployment record:

```powershell
$parameters = @{
    CurrentAppPath = $snapshotPath
    ImageManifestPath = '<private-path-to-images.json>'
    OutputDirectory = $operatorDirectory
    ExpectedRevision = '<current-full-revision-name>'
    RevisionSuffix = '<new-unique-suffix>'
    ModelOrigin = '<approved-https-Azure-OpenAI-origin>'
    ModelDeployment = '<existing-deployment-name>'
    ModelId = 'gpt-4.1-mini-2025-04-14'
    TenantId = $env:AZURE_TENANT_ID
    ClientId = $env:NEXUS_MANAGED_IDENTITY_CLIENT_ID
}
$prepared = .\infra\scripts\New-NetworkDemoPatch.ps1 @parameters
```

The helper **does not deploy**. It generates an update and rollback pair while enforcing the existing topology, resource sizes, selected identity, immutable registry images and model limits. It does not include authentication secrets, ingress changes, or identity assignments in the update.

It also handles two observed platform details:

- Recent CLI readbacks contain fields such as `imageType` that the stable write API rejects. Only the supported ordinary-container representation is exported.
- Container Apps supplies both modern `IDENTITY_*` and legacy `MSI_*` aliases. Startup first requires equal alias values, removes only the redundant legacy pair in the backend process, and selects the explicit App Service identity. Other identity-source guards remain in force.

The generated startup command uses LF line endings for the Linux container, even when prepared on Windows.

After obtaining approval, confirming there are no active queries, and checking that the expected live revision has not changed:

```powershell
$snapshot = Get-Content $parameters.CurrentAppPath -Raw | ConvertFrom-Json
$resourceUrl = "https://management.azure.com$($snapshot.id)?api-version=2025-01-01"
az rest --method patch --url $resourceUrl --headers Content-Type=application/json --body "@$($prepared.PatchPath)"
if ($LASTEXITCODE -ne 0) { throw 'Inspect the resource before retrying an unacknowledged update.' }
```

Do not declare success from an ARM acknowledgement alone. Check the actual ready revision and replicas, authentication behavior, a real query, and its committed result.

### Rollback and cost boundaries

The generated rollback file restores the prior images and template through a new revision suffix. Apply it to the same resource only after confirming the target and impact:

```powershell
az rest --method patch --url $resourceUrl --headers Content-Type=application/json --body "@$($prepared.RollbackPath)"
```

Rolling revisions can clear process-local history. Preserve failure evidence first. Do not delete resource groups, replace the original release tag, rotate passwords, or remove pre-existing role assignments as an incidental cleanup step.

| Meter | Boundary |
| --- | --- |
| Container Apps | One always-on replica continues to incur its normal compute cost |
| ACR Tasks | Image builds are separately metered; this handoff allowed three one-attempt builds, each at most 1,800 seconds |
| Azure OpenAI | Real calls use the existing model deployment and its pricing |
| GitHub Actions | A separate allowance; using ACR does not replenish it or make Azure builds free |
| Existing unrelated/future resources | Not created, resized or deleted by this upgrade recipe |

The live-check budget was at most 20 model HTTP requests, including repairs, at concurrency one. Subsequent human use is separately metered. A zero token count in a failed run does not prove that Azure performed no billable work; actual billing remains authoritative.

## 8. Checks and reproducible evidence

Run targeted local checks from the repository root:

```powershell
pnpm --dir apps\web test ask-workflow http-semantic-nexus-client
pnpm --dir apps\web run build

.\.venv\Scripts\python.exe -m pytest services\semantic-api\tests\test_provider.py services\semantic-api\tests\test_structured_provider.py services\semantic-backend\tests\test_provider_configuration.py -o asyncio_mode=auto -q
.\.venv\Scripts\python.exe -m pytest -c services\semantic-backend\pyproject.toml services\semantic-backend\tests -q

dotnet test services\control-api\tests\ControlApi.Tests\ControlApi.Tests.csproj --filter "FullyQualifiedName~SemanticBackendClientTests|FullyQualifiedName~ApiEndpointTests|FullyQualifiedName~RunServiceTests"
.\infra\scripts\test-network-demo-patch.ps1
.\.venv\Scripts\python.exe scripts\clean_room_scan.py --repository .
```

The cross-package Python command explicitly uses the packages' existing asyncio auto mode. Running it under an unrelated root pytest configuration without that mode does not execute the async tests correctly.

The live browser check is separate from ordinary tests and has no automatic retry. It requires explicit permission and HTTPS credentials:

```powershell
$credential = Get-Credential
$env:NEXUS_TEST_USERNAME = $credential.UserName
$storedValue = $credential.GetNetworkCredential().Password
$env:NEXUS_TEST_PASSWORD = $storedValue
$env:NEXUS_ALLOW_NETWORK_DEMO = '1'
$env:NEXUS_NETWORK_DEMO_URL = '<approved-HTTPS-origin>'
# Obtain this value from the deployed image manifest, not an unrelated local HEAD.
$env:NEXUS_DEMO_SOURCE_COMMIT = '<deployed-image-source-commit>'
try {
    pnpm --dir apps\web run test:e2e:network-demo --headed --output '<private-evidence-directory>'
} finally {
    Remove-Item Env:\NEXUS_TEST_USERNAME,Env:\NEXUS_TEST_PASSWORD,Env:\NEXUS_ALLOW_NETWORK_DEMO,Env:\NEXUS_NETWORK_DEMO_URL,Env:\NEXUS_DEMO_SOURCE_COMMIT -ErrorAction SilentlyContinue
    Remove-Variable credential,storedValue
}
```

The check uses the actual public browser flow, not intercepted fixture responses. It verifies the committed 20-row result, server model/token provenance, run-detail navigation, lineage and manifest; it saves screenshots and a small evidence record. Trace and video capture are disabled to avoid collecting authentication material.

Local and deployed evidence do not stand in for the full hosted release gate, PostgreSQL integration matrix, production identity review, or license/operations approval. Those gates remain separate.

## 9. Troubleshooting

| Symptom | Meaning and next action |
| --- | --- |
| HTTP 401 at the public entry | Expected without the demo login; obtain the existing credentials privately |
| `CONTEXT_CONCEPT_NOT_SELECTED` | The graph needs a concept not selected into the allowed context; use the supported example or investigate the semantic model, not weaker validation |
| `PROVIDER_SCHEMA` | Model output failed local schema validation and was not executed; inspect the run, retry deliberately, and do not treat this as a committed answer |
| `PROVIDER_AUTH_UNAVAILABLE` | Identity-source selection is invalid or ambiguous; inspect selector presence without printing token/header values |
| Healthy app but unsuccessful question | Readiness checks do not perform billable model inference; inspect the actual run outcome |
| Linux reports an illegal shell option | Check that the generated startup command contains LF, not Windows CRLF |
| Azure rejects `imageType` | Do not send an unfiltered modern CLI readback to the older write API; use the guarded patch helper |
| No run history after restart | Expected for this preview's memory-backed history; query again rather than treating old run URLs as durable records |
| CI is red before any job starts | Check the Actions account allowance separately from application health; do not silently change billing or bypass protection |

## 10. Positioning and alternatives

The common task is controlled analysis of structured data, not unrestricted conversation.

| Approach | Appropriate use | Trade-off relevant to this project |
| --- | --- | --- |
| Fixed SQL or BI report | Stable, well-defined questions and reporting rules | A strong baseline when natural-language interpretation adds little value |
| Model-assisted SQL with independent controls | Flexible query authoring | Still needs explicit authorization, validation, execution limits and operational safeguards |
| This governed query-graph preview | Inspecting how language interpretation can be separated from checked execution | More explicit intermediate structure, but currently limited scenarios and incomplete product operations |
| A mature analytics/data platform | Broader integrated product requirements | Evaluate the actual product's capabilities, deployment needs and cost rather than assuming this preview has feature parity |

No performance, accuracy or cost superiority over those alternatives is established here.

## 11. Next milestones and contribution

Keep the complete product objective, but accept one integrated user flow at a time:

1. Stabilize and repeat the current public core flow, including useful diagnostics for rejected model output.
2. Integrate reviewed persisted runs/results and prove restart/recovery behavior.
3. Join ontology authoring, publication, resource authorization and Ask into one version.
4. Integrate knowledge, datasets, administration and commercial-operation modules with their real APIs.
5. Complete production identity, private data-plane access, backup/restore, load, cost and license/release evidence.

These are remaining milestones, not delivered features. Existing work in other branches should be integrated and reviewed, not discarded or represented by new mock pages.

Contributions follow [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and the [clean-room policy](docs/adr/0001-clean-room-policy.md). The project uses the [Apache License 2.0](LICENSE); a repository license is not proof that every future dependency or distribution variant has completed its separate license review.
