#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$CurrentAppPath,
    [Parameter(Mandatory)][string]$ImageManifestPath,
    [Parameter(Mandatory)][string]$OutputDirectory,
    [Parameter(Mandatory)][string]$ExpectedRevision,
    [Parameter(Mandatory)][ValidatePattern('^[a-z][a-z0-9-]{0,40}$')][string]$RevisionSuffix,
    [Parameter(Mandatory)][ValidatePattern('^https://[a-z0-9-]+\.openai\.azure\.com$')][string]$ModelOrigin,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_.-]{1,100}$')][string]$ModelDeployment,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_.-]{1,100}$')][string]$ModelId,
    [Parameter(Mandatory)][guid]$TenantId,
    [Parameter(Mandatory)][guid]$ClientId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$current = Get-Content -LiteralPath $CurrentAppPath -Raw | ConvertFrom-Json -AsHashtable
$manifest = Get-Content -LiteralPath $ImageManifestPath -Raw | ConvertFrom-Json -AsHashtable
if ($current.properties.latestRevisionName -ne $ExpectedRevision) {
    throw 'The current revision differs from the approved rollback baseline.'
}
if ($current.properties.configuration.activeRevisionsMode -ne 'Single' -or
    $current.properties.configuration.ingress.targetPort -ne 8080 -or
    $current.properties.configuration.ingress.allowInsecure -ne $false) {
    throw 'This recipe requires the existing HTTPS, single-revision demo topology.'
}
if ($manifest.sourceCommit -notmatch '^[0-9a-f]{40}$') {
    throw 'The image manifest must identify a fixed source commit.'
}
$identityClients = @($current.identity.userAssignedIdentities.Values | ForEach-Object { $_.clientId })
if ($ClientId.ToString() -notin $identityClients) {
    throw 'The selected managed identity is not attached to the existing application.'
}

$original = $current.properties.template | ConvertTo-Json -Depth 80 | ConvertFrom-Json -AsHashtable
$candidate = $current.properties.template | ConvertTo-Json -Depth 80 | ConvertFrom-Json -AsHashtable
if ($candidate.scale.minReplicas -ne 1 -or $candidate.scale.maxReplicas -ne 1) {
    throw 'This recipe preserves exactly one always-on replica; it cannot change scale.'
}
$expectedResources = @{
    'web' = @{ cpu = 0.25; memory = '0.5Gi' }
    'control-api' = @{ cpu = 0.25; memory = '0.5Gi' }
    'semantic-backend' = @{ cpu = 0.5; memory = '1Gi' }
}
$names = @($candidate.containers | ForEach-Object { $_.name })
if ($names.Count -ne 3 -or @($names | Select-Object -Unique).Count -ne 3) {
    throw 'The existing three-container topology must remain unchanged.'
}
$registries = @($current.properties.configuration.registries | ForEach-Object { $_.server })
foreach ($container in $candidate.containers) {
    $expected = $expectedResources[$container.name]
    if ($null -eq $expected -or $container.resources.cpu -ne $expected.cpu -or
        $container.resources.memory -ne $expected.memory) {
        throw 'The existing container resource allocation differs from the approved scope.'
    }
    $image = $manifest.images[$container.name]
    if ($image -notmatch '^([^/]+)/([^/@]+)@sha256:([0-9a-f]{64})$' -or
        $Matches[1] -notin $registries -or $Matches[2] -ne $container.name) {
        throw "An immutable image in the existing registry is required for $($container.name)."
    }
    $container.image = $image
    $container.resources.Remove('ephemeralStorage')
}
foreach ($container in $original.containers) {
    $container.resources.Remove('ephemeralStorage')
}

$backend = @($candidate.containers | Where-Object { $_.name -eq 'semantic-backend' })[0]
$environment = [ordered]@{}
foreach ($entry in $backend.env) {
    if (-not $entry.name.StartsWith('SEMANTIC_COMPILER_', [StringComparison]::Ordinal)) {
        $environment[$entry.name] = $entry
    }
}
$settings = [ordered]@{
    SEMANTIC_NEXUS_AUTH_MODE = 'legacy-development'
    SEMANTIC_NEXUS_ENVIRONMENT = 'Development'
    SEMANTIC_COMPILER_MODE = 'azure_openai'
    SEMANTIC_COMPILER_ENDPOINT = "$ModelOrigin/openai/v1/chat/completions"
    SEMANTIC_COMPILER_AZURE_APPROVED_ORIGIN = $ModelOrigin
    SEMANTIC_COMPILER_AZURE_DEPLOYMENT = $ModelDeployment
    SEMANTIC_COMPILER_MODEL = $ModelId
    SEMANTIC_COMPILER_AZURE_AUTH = 'managed_identity'
    SEMANTIC_COMPILER_AZURE_TENANT_ID = $TenantId.ToString()
    SEMANTIC_COMPILER_AZURE_CLIENT_ID = $ClientId.ToString()
    SEMANTIC_COMPILER_AZURE_MANAGED_IDENTITY_SOURCE = 'app_service'
    SEMANTIC_COMPILER_TIMEOUT_SECONDS = '20'
    SEMANTIC_COMPILER_MAX_INPUT_TOKENS = '32768'
    SEMANTIC_COMPILER_MAX_OUTPUT_TOKENS = '2048'
    SEMANTIC_COMPILER_MAX_CONCURRENT_CALLS = '1'
}
foreach ($entry in $settings.GetEnumerator()) {
    $environment[$entry.Key] = @{ name = $entry.Key; value = $entry.Value }
}
$backend.env = @($environment.Values)
$backend.command = @('sh', '-c')
$backend.args = @(
    'set -eu; : "${IDENTITY_ENDPOINT:?Managed identity endpoint is missing}"; export SEMANTIC_COMPILER_AZURE_MANAGED_IDENTITY_ENDPOINT="$IDENTITY_ENDPOINT"; exec python -m uvicorn semantic_backend.api:create_app --factory --host 127.0.0.1 --port 8082'
)
$candidate.revisionSuffix = $RevisionSuffix
$original.revisionSuffix = "$RevisionSuffix-rollback"

# A template-only merge patch leaves authentication secrets, ingress and identities untouched.
$patch = @{ location = $current.location; properties = @{ template = $candidate } }
$rollback = @{ location = $current.location; properties = @{ template = $original } }
$patchPath = Join-Path $OutputDirectory "$RevisionSuffix.patch.json"
$rollbackPath = Join-Path $OutputDirectory "$RevisionSuffix.rollback.json"
if ((Test-Path -LiteralPath $patchPath) -or (Test-Path -LiteralPath $rollbackPath)) {
    throw 'Refusing to overwrite an existing deployment or rollback record.'
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$patch | ConvertTo-Json -Depth 80 | Out-File -LiteralPath $patchPath -Encoding utf8
$rollback | ConvertTo-Json -Depth 80 | Out-File -LiteralPath $rollbackPath -Encoding utf8
[pscustomobject]@{
    SourceCommit = $manifest.sourceCommit
    ExpectedRevision = $ExpectedRevision
    PatchPath = $patchPath
    RollbackPath = $rollbackPath
    Applied = $false
}
