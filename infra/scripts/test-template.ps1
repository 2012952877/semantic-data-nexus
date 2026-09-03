[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Read-RepoFile {
    param([string]$RelativePath)

    return Get-Content -LiteralPath (Join-Path $repoRoot $RelativePath) -Raw
}

function Assert-Pattern {
    param(
        [string]$Content,
        [string]$Pattern,
        [string]$Message
    )

    if ($Content -notmatch $Pattern) {
        throw $Message
    }
}

$main = Read-RepoFile 'infra\bicep\main.bicep'
$containerApps = Read-RepoFile 'infra\bicep\modules\container-apps.bicep'
$acrPull = Read-RepoFile 'infra\bicep\modules\acr-pull.bicep'
$postgres = Read-RepoFile 'infra\bicep\modules\postgres.bicep'
$readme = Read-RepoFile 'infra\README.md'
$validateScript = Read-RepoFile 'infra\scripts\validate.ps1'
$runtimeParameterScript = Join-Path $repoRoot 'infra\scripts\New-RuntimeParameters.ps1'

Assert-Pattern $main "module acrPull 'modules/acr-pull\.bicep'" 'The pre-deployment ACR pull module is missing.'
Assert-Pattern $main '(?s)module containerApps .*?dependsOn:\s*\[\s*acrPull\s+observability\s*\]' 'Container Apps must depend on completed AcrPull assignments.'

$clientIdEnvironmentVariables = [regex]::Matches($containerApps, "name:\s*'AZURE_CLIENT_ID'").Count
if ($clientIdEnvironmentVariables -ne 4) {
    throw "Expected four AZURE_CLIENT_ID environment variables, found $clientIdEnvironmentVariables."
}

$managedIdentityClientIds = [regex]::Matches($containerApps, 'value:\s*\w+Identity\.properties\.clientId').Count
if ($managedIdentityClientIds -ne 4) {
    throw "Expected four managed identity client ID bindings, found $managedIdentityClientIds."
}

Assert-Pattern $containerApps "(?s)resource webApp .*?identity:\s*\{\s*type:\s*'UserAssigned'.*?'\$\{webIdentity\.id\}': \{\}" 'The web app must attach its user-assigned identity.'
Assert-Pattern $containerApps "(?s)resource webApp .*?registries:\s*\[\s*\{\s*identity:\s*webIdentity\.id" 'The web app must use its user-assigned identity for ACR pulls.'
Assert-Pattern $containerApps '(?s)var workerCommandOverride = useWorkerPlaceholderCommand \? \{.*?command:.*?args:.*?\} : \{\}' 'Worker placeholder command must be conditional.'
Assert-Pattern $containerApps '(?s)containers:\s*\[\s*union\(\{.*?name:\s*''worker''.*?\}, workerCommandOverride\)' 'Worker command override must be merged conditionally so real images keep their entrypoint.'

$acrPullAssignments = [regex]::Matches($acrPull, "roleDefinitionId:\s*subscriptionResourceId\('Microsoft\.Authorization/roleDefinitions', acrPullRoleId\)").Count
if ($acrPullAssignments -ne 4) {
    throw "Expected four pre-created AcrPull assignments, found $acrPullAssignments."
}

Assert-Pattern $main 'output keyVaultEndpoint string = keyVault\.outputs\.keyVaultEndpoint' 'Key Vault endpoint must use the module output.'
Assert-Pattern $postgres "(?s)resource firewallRuleResources 'Microsoft\.DBforPostgreSQL/flexibleServers/firewallRules@[^']+' = \[for \(ipAddress, index\) in allowedIpAddresses:" 'Parameterized PostgreSQL firewall resources are missing.'
Assert-Pattern $postgres '(?s)startIpAddress:\s*ipAddress\s+endIpAddress:\s*ipAddress' 'PostgreSQL firewall entries must allow exact IP addresses only.'
Assert-Pattern $postgres '(?s)resource database .*?dependsOn:\s*\[\s*entraAdministrator\s*\]' 'The PostgreSQL database must wait for the Entra administrator.'
Assert-Pattern $postgres '(?s)resource firewallRuleResources .*?dependsOn:\s*\[\s*database\s*\]' 'PostgreSQL firewall rules must wait for database creation.'

if ($postgres -match "startIpAddress:\s*'0\.0\.0\.0'" -or $postgres -match "endIpAddress:\s*'255\.255\.255\.255'") {
    throw 'A broad PostgreSQL firewall range must not be committed.'
}

Assert-Pattern $readme '-PostgresEntraAdministratorObjectId <object-id>' 'README what-if example must supply the PostgreSQL administrator object ID.'
Assert-Pattern $readme '-PostgresEntraAdministratorPrincipalName <display-name>' 'README what-if example must supply the PostgreSQL administrator principal name.'
Assert-Pattern $readme '-PostgresFirewallIpAddress <public-ip>' 'README what-if example must supply the exact PostgreSQL firewall IP.'
Assert-Pattern $readme 'az stack group create' 'README deployments must use an Azure deployment stack.'
Assert-Pattern $readme '(?s)az stack group create .*?--parameters \$runtimeParametersFile' 'README stack deployment must use only the merged runtime parameter file.'
Assert-Pattern $readme "--action-on-unmanage 'deleteResources'" 'Deployment stacks must delete resources removed from the template.'
Assert-Pattern $readme 'useWorkerPlaceholderCommand=false' 'README must preserve real worker image entrypoints.'
Assert-Pattern $readme 'A plain incremental `az deployment group create` does not delete removed rules' 'README must warn that incremental deployments retain removed firewall rules.'
Assert-Pattern $validateScript '(?s)az stack-whatif group create .*?--parameters \$runtimeParameters' 'Authenticated preview must use stack what-if with one merged parameter file.'

if ($readme -match '(?s)az stack group create .*?--parameters[^\r\n]*\.bicepparam') {
    throw 'README stack deployment must not mix a .bicepparam file with merged runtime JSON.'
}

if ($validateScript -match 'az deployment group what-if' -or $validateScript -match 'postgresAllowedIpAddresses=\$' -or $validateScript -match 'ConvertTo-Json\s+-Compress\s+-InputObject') {
    throw 'What-if must not encode PostgreSQL IP arrays as inline PowerShell arguments.'
}

$runtimeParametersFile = Join-Path ([System.IO.Path]::GetTempPath()) "$([guid]::NewGuid()).json"
$baseParametersFile = Join-Path ([System.IO.Path]::GetTempPath()) "$([guid]::NewGuid()).json"
try {
    az bicep build-params `
        --file (Join-Path $repoRoot 'infra\bicep\parameters\dev.bicepparam') `
        --outfile $baseParametersFile `
        --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to compile the base Bicep parameters for regression testing.'
    }

    & $runtimeParameterScript `
        -OutputPath $runtimeParametersFile `
        -BaseParametersPath $baseParametersFile `
        -PostgresEntraAdministratorObjectId '00000000-0000-0000-0000-000000000000' `
        -PostgresEntraAdministratorPrincipalName 'example-group' `
        -PostgresFirewallIpAddress '203.0.113.10'

    $runtimeParametersJson = Get-Content -LiteralPath $runtimeParametersFile -Raw
    $runtimeParameters = $runtimeParametersJson | ConvertFrom-Json
    $baseParameters = Get-Content -LiteralPath $baseParametersFile -Raw | ConvertFrom-Json
    $allowedIpAddresses = @($runtimeParameters.parameters.postgresAllowedIpAddresses.value)

    if ($allowedIpAddresses.Count -ne 1 -or $allowedIpAddresses[0] -ne '203.0.113.10') {
        throw 'Runtime parameter generation did not preserve a one-element IP array.'
    }
    if ($runtimeParametersJson -notmatch '(?s)"postgresAllowedIpAddresses".*?"value"\s*:\s*\[\s*"203\.0\.113\.10"\s*\]') {
        throw 'Runtime parameter JSON does not encode postgresAllowedIpAddresses as an array.'
    }

    if ($runtimeParameters.'$schema' -ne 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#' -or
        $runtimeParameters.contentVersion -ne '1.0.0.0') {
        throw 'Runtime parameters are not a valid ARM deployment parameter document.'
    }

    $baseParameterNames = @($baseParameters.parameters.PSObject.Properties.Name)
    $runtimeParameterNames = @($runtimeParameters.parameters.PSObject.Properties.Name)
    if ($baseParameterNames.Count -ne $runtimeParameterNames.Count -or
        @($baseParameterNames | Where-Object { $_ -notin $runtimeParameterNames }).Count -ne 0) {
        throw 'Runtime parameter generation must preserve the complete compiled Bicep parameter set.'
    }

    foreach ($parameter in $runtimeParameters.parameters.PSObject.Properties) {
        if ('value' -notin $parameter.Value.PSObject.Properties.Name) {
            throw "Runtime parameter '$($parameter.Name)' is missing its ARM value wrapper."
        }
    }

    & $runtimeParameterScript `
        -OutputPath $runtimeParametersFile `
        -BaseParametersPath $baseParametersFile `
        -PostgresEntraAdministratorObjectId '00000000-0000-0000-0000-000000000000' `
        -PostgresEntraAdministratorPrincipalName 'example-group' `
        -PostgresFirewallIpAddress @()

    $emptyRuntimeParametersJson = Get-Content -LiteralPath $runtimeParametersFile -Raw
    $emptyRuntimeParameters = $emptyRuntimeParametersJson | ConvertFrom-Json
    if (@($emptyRuntimeParameters.parameters.postgresAllowedIpAddresses.value).Count -ne 0 -or
        $emptyRuntimeParametersJson -notmatch '(?s)"postgresAllowedIpAddresses".*?"value"\s*:\s*\[\s*\]') {
        throw 'Runtime parameter generation must preserve an explicit empty array for full firewall revocation.'
    }

    try {
        & $runtimeParameterScript `
            -OutputPath $runtimeParametersFile `
            -BaseParametersPath $baseParametersFile `
            -PostgresEntraAdministratorObjectId '00000000-0000-0000-0000-000000000000' `
            -PostgresEntraAdministratorPrincipalName 'example-group' `
            -PostgresFirewallIpAddress '0.0.0.0'
        throw 'Runtime parameter generation accepted a broad PostgreSQL firewall sentinel.'
    }
    catch {
        if ($_.Exception.Message -notmatch 'all-address sentinels') {
            throw
        }
    }
}
finally {
    if (Test-Path -LiteralPath $runtimeParametersFile) {
        Remove-Item -LiteralPath $runtimeParametersFile -Force
    }
    if (Test-Path -LiteralPath $baseParametersFile) {
        Remove-Item -LiteralPath $baseParametersFile -Force
    }
}

Write-Host 'Infrastructure regression assertions passed: identity ordering, worker entrypoint, endpoint, firewall reconciliation docs, and structural ARM parameter JSON.'
