#requires -Version 7.0
$ErrorActionPreference = 'Stop'
$directory = Join-Path ([IO.Path]::GetTempPath()) ("nexus-patch-test-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $directory | Out-Null
try {
    $client = [guid]::Empty.ToString()
    $containers = @(
        @{ name = 'web'; image = 'demo.azurecr.io/web:old'; imageType = 'ContainerImage'; resources = @{ cpu = 0.25; memory = '0.5Gi' } }
        @{ name = 'control-api'; image = 'demo.azurecr.io/control-api:old'; resources = @{ cpu = 0.25; memory = '0.5Gi' } }
        @{
            name = 'semantic-backend'
            image = 'demo.azurecr.io/semantic-backend:old'
            resources = @{ cpu = 0.5; memory = '1Gi'; ephemeralStorage = '2Gi' }
            env = @(@{ name = 'SEMANTIC_NEXUS_RESOLVER'; value = 'fake' })
        }
    )
    $current = @{
        location = 'eastus2'
        identity = @{ userAssignedIdentities = @{ 'existing-identity' = @{ clientId = $client } } }
        properties = @{
            latestRevisionName = 'demo--old'
            configuration = @{
                activeRevisionsMode = 'Single'
                ingress = @{ targetPort = 8080; allowInsecure = $false }
                registries = @(@{ server = 'demo.azurecr.io' })
            }
            template = @{
                scale = @{ minReplicas = 1; maxReplicas = 1 }
                containers = $containers
                volumes = @(@{ name = 'auth'; storageType = 'Secret' })
                revisionSuffix = 'old'
                customMetricsSettings = $null
            }
        }
    }
    $manifest = @{
        sourceCommit = 'a' * 40
        images = @{
            'web' = 'demo.azurecr.io/web@sha256:' + ('b' * 64)
            'control-api' = 'demo.azurecr.io/control-api@sha256:' + ('c' * 64)
            'semantic-backend' = 'demo.azurecr.io/semantic-backend@sha256:' + ('d' * 64)
        }
    }
    $currentPath = Join-Path $directory 'current.json'
    $manifestPath = Join-Path $directory 'images.json'
    $current | ConvertTo-Json -Depth 20 | Out-File -LiteralPath $currentPath -Encoding utf8
    $manifest | ConvertTo-Json -Depth 10 | Out-File -LiteralPath $manifestPath -Encoding utf8
    $parameters = @{
        CurrentAppPath = $currentPath
        ImageManifestPath = $manifestPath
        OutputDirectory = $directory
        ExpectedRevision = 'demo--old'
        RevisionSuffix = 'network-test'
        ModelOrigin = 'https://synthetic.openai.azure.com'
        ModelDeployment = 'synthetic-deployment'
        ModelId = 'gpt-4.1-mini-2025-04-14'
        TenantId = [guid]::Empty
        ClientId = $client
    }
    $result = & (Join-Path $PSScriptRoot 'New-NetworkDemoPatch.ps1') @parameters
    $patch = Get-Content -LiteralPath $result.PatchPath -Raw | ConvertFrom-Json -AsHashtable
    $rollback = Get-Content -LiteralPath $result.RollbackPath -Raw | ConvertFrom-Json -AsHashtable
    if ($patch.properties.ContainsKey('configuration') -or $patch.ContainsKey('identity')) {
        throw 'Authentication or identity configuration leaked into the patch.'
    }
    if ($patch.properties.template.ContainsKey('customMetricsSettings') -or
        $patch.properties.template.containers[0].ContainsKey('imageType')) {
        throw 'Unsupported readback fields leaked into the stable API patch.'
    }
    $backend = @($patch.properties.template.containers | Where-Object name -eq 'semantic-backend')[0]
    if ($backend.args[0] -notmatch 'unset MSI_ENDPOINT MSI_SECRET' -or
        $backend.args[0] -notmatch 'Conflicting managed identity endpoint aliases' -or
        $backend.args[0] -notmatch 'Conflicting managed identity header aliases') {
        throw 'Explicit identity selection must reject conflicting legacy aliases.'
    }
    if ($backend.args[0].Contains("`r")) {
        throw 'The Linux startup script must use LF line endings.'
    }
    $envValues = @{}
    foreach ($entry in $backend.env) { $envValues[$entry.name] = $entry.value }
    if ($envValues.SEMANTIC_COMPILER_MAX_CONCURRENT_CALLS -ne '1' -or
        $envValues.SEMANTIC_COMPILER_MAX_INPUT_TOKENS -ne '32768' -or
        $envValues.SEMANTIC_COMPILER_MAX_OUTPUT_TOKENS -ne '2048' -or
        $envValues.SEMANTIC_COMPILER_MODE -ne 'azure_openai' -or
        $patch.properties.template.scale.maxReplicas -ne 1) {
        throw 'The approved model or compute bounds changed.'
    }
    if ($rollback.properties.template.containers[0].image -ne 'demo.azurecr.io/web:old' -or
        $rollback.properties.template.revisionSuffix -ne 'network-test-rollback') {
        throw 'Rollback did not preserve the original images.'
    }
    $parameters.RevisionSuffix = 'invalid-test'
    $manifest.images.web = 'other.azurecr.io/web:mutable'
    $manifest | ConvertTo-Json -Depth 10 | Out-File -LiteralPath $manifestPath -Encoding utf8
    try {
        & (Join-Path $PSScriptRoot 'New-NetworkDemoPatch.ps1') @parameters | Out-Null
        throw 'Expected an invalid-image rejection.'
    } catch {
        if ($_.Exception.Message -notlike '*immutable image*') { throw }
    }
    Write-Output 'Network demo patch preserves topology/auth, enforces bounds, and rejects mutable/foreign images.'
} finally {
    Remove-Item -LiteralPath $directory -Recurse -Force
}
