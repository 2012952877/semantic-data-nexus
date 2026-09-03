[CmdletBinding()]
param(
    [string]$ResourceGroup,
    [ValidateSet('dev', 'prod')]
    [string]$Environment = 'dev',
    [string]$PostgresEntraAdministratorObjectId,
    [string]$PostgresEntraAdministratorPrincipalName
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$templateFile = Join-Path $repoRoot 'infra\bicep\main.bicep'
$parameterFile = Join-Path $repoRoot "infra\bicep\parameters\$Environment.bicepparam"
$compiledParameters = [System.IO.Path]::ChangeExtension([System.IO.Path]::GetTempFileName(), '.json')

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

    if ([string]::IsNullOrWhiteSpace($PostgresEntraAdministratorObjectId) -or
        [string]::IsNullOrWhiteSpace($PostgresEntraAdministratorPrincipalName)) {
        throw 'PostgreSQL Entra administrator object ID and principal name are required for what-if.'
    }

    az account show --only-show-errors | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Azure authentication is required for what-if.'
    }

    az deployment group what-if `
        --resource-group $ResourceGroup `
        --template-file $templateFile `
        --parameters $parameterFile `
        --parameters postgresEntraAdministratorObjectId=$PostgresEntraAdministratorObjectId `
                     postgresEntraAdministratorPrincipalName=$PostgresEntraAdministratorPrincipalName `
        --no-pretty-print `
        --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw 'Azure deployment what-if failed.'
    }
}
finally {
    if (Test-Path -LiteralPath $compiledParameters) {
        Remove-Item -LiteralPath $compiledParameters -Force
    }
}
