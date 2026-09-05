[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Read-CompiledTemplate {
    param([string]$Path)
    $result = az bicep build --file (Join-Path $repoRoot $Path) --stdout --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to compile $Path."
    }
    return ($result | ConvertFrom-Json)
}

function Assert-Condition {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw $Message
    }
}

$foundation = Read-CompiledTemplate 'infra\bicep\m0-aca-foundation.bicep'
$template = Read-CompiledTemplate 'infra\bicep\m0-aca.bicep'
$environment = $foundation.resources | Where-Object type -eq 'Microsoft.App/managedEnvironments'
$registry = $foundation.resources | Where-Object type -eq 'Microsoft.ContainerRegistry/registries'
Assert-Condition (-not $environment.properties.PSObject.Properties['appLogsConfiguration'] -and
    -not $environment.properties.PSObject.Properties['vnetConfiguration'] -and
    $environment.properties.workloadProfiles.Count -eq 1 -and
    $environment.properties.workloadProfiles[0].workloadProfileType -eq 'Consumption') `
    'The test environment must have only Consumption and no paid log/network configuration.'
Assert-Condition ($registry.sku.name -eq 'Basic' -and
    $registry.properties.adminUserEnabled -eq $false -and
    $registry.properties.anonymousPullEnabled -eq $false) 'Use a private, admin-disabled Basic registry.'
Assert-Condition (@($foundation.resources | Where-Object {
    $_.type -notin @(
        'Microsoft.App/managedEnvironments', 'Microsoft.ContainerRegistry/registries',
        'Microsoft.ManagedIdentity/userAssignedIdentities', 'Microsoft.Resources/deployments'
    )
}).Count -eq 0) 'The hosted test must not create baseline databases, search, or model services.'

$app = @($template.resources | Where-Object type -eq 'Microsoft.App/containerApps')
Assert-Condition ($app.Count -eq 1) 'Use exactly one container app.'
$app = $app[0]
$ingress = $app.properties.configuration.ingress
Assert-Condition ($ingress.external -eq $true -and $ingress.allowInsecure -eq $false -and
    $ingress.targetPort -eq 8080 -and
    -not $ingress.PSObject.Properties['additionalPortMappings']) 'Only the protected Nginx HTTPS entry may be exposed.'
Assert-Condition ($app.properties.configuration.activeRevisionsMode -eq 'Single' -and
    $app.properties.template.scale.minReplicas -eq 0 -and
    $app.properties.template.scale.maxReplicas -eq 1) 'Keep one process-local replica and scale idle tests to zero.'
Assert-Condition ($template.parameters.authFile.type -eq 'secureString' -and
    -not $template.parameters.authFile.PSObject.Properties['defaultValue']) 'Authentication must be supplied before the first revision.'
Assert-Condition ($template.parameters.fakeDelayMs.defaultValue -eq 750 -and
    $template.parameters.fakeDelayMs.minValue -eq 0 -and
    $template.parameters.fakeDelayMs.maxValue -eq 5000) 'Synthetic demo latency must preserve the local default and remain bounded.'
Assert-Condition ($app.properties.template.volumes.Count -eq 2 -and
    @($app.properties.template.volumes | Where-Object storageType -ne 'Secret').Count -eq 0) `
    'Use secret volumes for the password hash and authenticated Nginx configuration.'

$containers = @($app.properties.template.containers)
Assert-Condition ($containers.Count -eq 3) 'Run the three real application containers together.'
$web = $containers | Where-Object name -eq 'web'
$bff = $containers | Where-Object name -eq 'control-api'
$backend = $containers | Where-Object name -eq 'semantic-backend'
Assert-Condition ($web.resources.cpu -eq "[json('0.25')]" -and
    $bff.resources.cpu -eq "[json('0.25')]" -and
    $backend.resources.cpu -eq "[json('0.5')]" -and
    $web.resources.memory -eq '0.5Gi' -and
    $bff.resources.memory -eq '0.5Gi' -and $backend.resources.memory -eq '1Gi') `
    'The approved test replica is 1 vCPU and 2 GiB total.'
Assert-Condition (@($web.probes | Where-Object { $_.httpGet.port -ne 8083 }).Count -eq 0) `
    'Probe traffic must use the unexposed health port.'
Assert-Condition (($bff.env | Where-Object name -eq 'ASPNETCORE_URLS').value -eq 'http://127.0.0.1:8081' -and
    ($bff.env | Where-Object name -eq 'SemanticBackend__BaseUri').value -eq 'http://127.0.0.1:8082/' -and
    ($bff.env | Where-Object name -eq 'SemanticBackend__UseFake').value -eq 'false') `
    'The BFF must bind to loopback and use the real loopback semantic backend.'
Assert-Condition (($backend.command -join ' ') -eq 'python -m uvicorn' -and
    ($backend.args -join ' ') -eq 'semantic_backend.api:create_app --factory --host 127.0.0.1 --port 8082' -and
    ($backend.env | Where-Object name -eq 'SEMANTIC_NEXUS_RESOLVER').value -eq 'fake') `
    'Only the real backend with synthetic resolver may run, bound to loopback.'

$nginx = Get-Content -LiteralPath (Join-Path $repoRoot 'infra\bicep\config\nginx.m0-aca.conf') -Raw
$publicServer = ($nginx -split '# This port is for ACA probes only;')[0]
foreach ($required in @(
    'map_hash_bucket_size 128;',
    'server_names_hash_bucket_size 128;',
    'auth_basic "Semantic Data Nexus synthetic test";',
    'auth_basic_user_file /etc/nexus-auth/htpasswd;',
    'proxy_set_header Authorization "";',
    'proxy_set_header X-Dev-Subject "local-compose-user";',
    'proxy_set_header X-Dev-Roles "reader,contributor,admin";',
    'if ($m0_origin_allowed = 0)', '__APP_HOST_REGEX__'
)) {
    Assert-Condition ($publicServer.Contains($required)) "Missing public authentication boundary: $required"
}
Assert-Condition ($publicServer -notmatch 'auth_basic\s+off' -and
    $publicServer -notmatch 'return\s+20[0-9]') 'No public location may bypass authentication through an early success return.'

Write-Host 'M0 ACA assertions passed: authenticated first revision, single HTTPS ingress, loopback services, secret volumes, and bounded Consumption footprint.'
