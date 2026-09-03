[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$OutputPath,

    [Parameter(Mandatory)]
    [string]$BaseParametersPath,

    [Parameter(Mandatory)]
    [string]$PostgresEntraAdministratorObjectId,

    [Parameter(Mandatory)]
    [string]$PostgresEntraAdministratorPrincipalName,

    [Parameter(Mandatory)]
    [AllowEmptyCollection()]
    [string[]]$PostgresFirewallIpAddress
)

$ErrorActionPreference = 'Stop'

foreach ($ipAddress in $PostgresFirewallIpAddress) {
    $parsedIpAddress = $null
    $isValidIpAddress = [System.Net.IPAddress]::TryParse($ipAddress, [ref]$parsedIpAddress)
    if (-not $isValidIpAddress -or
        $parsedIpAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $ipAddress -in @('0.0.0.0', '255.255.255.255')) {
        throw 'PostgresFirewallIpAddress values must be exact IPv4 addresses and cannot be all-address sentinels.'
    }
}

$document = Get-Content -LiteralPath $BaseParametersPath -Raw | ConvertFrom-Json
$document.parameters.postgresEntraAdministratorObjectId.value = $PostgresEntraAdministratorObjectId
$document.parameters.postgresEntraAdministratorPrincipalName.value = $PostgresEntraAdministratorPrincipalName
$document.parameters.postgresAllowedIpAddresses.value = [object[]]@($PostgresFirewallIpAddress)

$json = $document | ConvertTo-Json -Depth 6
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($OutputPath, $json, $utf8WithoutBom)
