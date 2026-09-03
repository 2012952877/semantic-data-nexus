[CmdletBinding()]
param(
    [string]$ResourceGroup,
    [ValidateSet('dev', 'prod')]
    [string]$Environment = 'dev',
    [string]$PostgresEntraAdministratorObjectId,
    [string]$PostgresEntraAdministratorPrincipalName,
    [string[]]$PostgresFirewallIpAddress,
    [string]$StackName
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$templateFile = Join-Path $repoRoot 'infra\bicep\main.bicep'
$parameterFile = Join-Path $repoRoot "infra\bicep\parameters\$Environment.bicepparam"
$compiledParameters = Join-Path ([System.IO.Path]::GetTempPath()) "$([guid]::NewGuid()).json"
$runtimeParameters = Join-Path ([System.IO.Path]::GetTempPath()) "$([guid]::NewGuid()).json"
$runtimeParameterScript = Join-Path $PSScriptRoot 'New-RuntimeParameters.ps1'

try {
    az bicep version --only-show-errors | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to run the Bicep CLI.'
    }

    az bicep build --file $templateFile --stdout --only-show-errors | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Bicep template build failed.'
    }

    az bicep build-params --file $parameterFile --outfile $compiledParameters --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw "Bicep parameter validation failed for $Environment."
    }

    if ([string]::IsNullOrWhiteSpace($ResourceGroup)) {
        Write-Host 'Static Bicep build and parameter validation succeeded. What-if skipped because -ResourceGroup was not supplied.'
        return
    }

    if ([string]::IsNullOrWhiteSpace($StackName)) {
        $StackName = "sdn-$Environment"
    }

    if ([string]::IsNullOrWhiteSpace($PostgresEntraAdministratorObjectId) -or
        [string]::IsNullOrWhiteSpace($PostgresEntraAdministratorPrincipalName) -or
        -not $PSBoundParameters.ContainsKey('PostgresFirewallIpAddress')) {
        throw 'PostgreSQL Entra administrator and an explicit firewall selection are required for what-if. Use @() to preview removal of all managed rules.'
    }

    & $runtimeParameterScript `
        -OutputPath $runtimeParameters `
        -BaseParametersPath $compiledParameters `
        -PostgresEntraAdministratorObjectId $PostgresEntraAdministratorObjectId `
        -PostgresEntraAdministratorPrincipalName $PostgresEntraAdministratorPrincipalName `
        -PostgresFirewallIpAddress $PostgresFirewallIpAddress

    az stack-whatif group create --help 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Install an Azure CLI release that includes the GA az stack-whatif command before running an authenticated preview.'
    }

    $subscriptionId = az account show --query id --output tsv --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw 'Azure authentication is required for what-if.'
    }

    $stackId = "/subscriptions/$subscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Resources/deploymentStacks/$StackName"
    $whatIfResultName = "$StackName-preview"

    az stack-whatif group create `
        --name $whatIfResultName `
        --resource-group $ResourceGroup `
        --stack-id $stackId `
        --template-file $templateFile `
        --parameters $runtimeParameters `
        --action-on-unmanage deleteResources `
        --deny-settings-mode none `
        --retention-interval PT3H `
        --no-pretty-print `
        --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw 'Azure deployment stack what-if failed.'
    }
}
finally {
    if (Test-Path -LiteralPath $compiledParameters) {
        Remove-Item -LiteralPath $compiledParameters -Force
    }
    if (Test-Path -LiteralPath $runtimeParameters) {
        Remove-Item -LiteralPath $runtimeParameters -Force
    }
}
