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

$acrPullAssignments = [regex]::Matches($acrPull, "roleDefinitionId:\s*subscriptionResourceId\('Microsoft\.Authorization/roleDefinitions', acrPullRoleId\)").Count
if ($acrPullAssignments -ne 4) {
    throw "Expected four pre-created AcrPull assignments, found $acrPullAssignments."
}

Assert-Pattern $main 'output keyVaultEndpoint string = keyVault\.outputs\.keyVaultEndpoint' 'Key Vault endpoint must use the module output.'
Assert-Pattern $postgres "(?s)resource firewallRuleResources 'Microsoft\.DBforPostgreSQL/flexibleServers/firewallRules@[^']+' = \[for \(ipAddress, index\) in allowedIpAddresses:" 'Parameterized PostgreSQL firewall resources are missing.'
Assert-Pattern $postgres '(?s)startIpAddress:\s*ipAddress\s+endIpAddress:\s*ipAddress' 'PostgreSQL firewall entries must allow exact IP addresses only.'

if ($postgres -match "startIpAddress:\s*'0\.0\.0\.0'" -or $postgres -match "endIpAddress:\s*'255\.255\.255\.255'") {
    throw 'A broad PostgreSQL firewall range must not be committed.'
}

Assert-Pattern $readme '-PostgresEntraAdministratorObjectId <object-id>' 'README what-if example must supply the PostgreSQL administrator object ID.'
Assert-Pattern $readme '-PostgresEntraAdministratorPrincipalName <display-name>' 'README what-if example must supply the PostgreSQL administrator principal name.'
Assert-Pattern $readme '-PostgresFirewallIpAddress <public-ip>' 'README what-if example must supply the exact PostgreSQL firewall IP.'

Write-Host 'Infrastructure regression assertions passed: ACR ordering, identity selection, Key Vault endpoint, and PostgreSQL connectivity.'
