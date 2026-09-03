[CmdletBinding()]
param(
    [string]$ResourceGroup,
    [ValidateSet('dev', 'prod')]
    [string]$Environment = 'dev',
    [string]$PostgresEntraAdministratorObjectId,
    [string]$PostgresEntraAdministratorPrincipalName,
    [string]$PostgresFirewallIpAddress
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
        [string]::IsNullOrWhiteSpace($PostgresEntraAdministratorPrincipalName) -or
        [string]::IsNullOrWhiteSpace($PostgresFirewallIpAddress)) {
        throw 'PostgreSQL Entra administrator and exact firewall IP parameters are required for what-if.'
    }

    $parsedIpAddress = $null
    $isValidIpAddress = [System.Net.IPAddress]::TryParse($PostgresFirewallIpAddress, [ref]$parsedIpAddress)
    if (-not $isValidIpAddress -or
        $parsedIpAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $PostgresFirewallIpAddress -in @('0.0.0.0', '255.255.255.255')) {
        throw 'PostgresFirewallIpAddress must be one exact, routable IPv4 address.'
    }

    $allowedIpAddresses = ConvertTo-Json -Compress -InputObject @($PostgresFirewallIpAddress)

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
                     "postgresAllowedIpAddresses=$allowedIpAddresses" `
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
