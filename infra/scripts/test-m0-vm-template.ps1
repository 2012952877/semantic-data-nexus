[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$compiledFile = Join-Path ([System.IO.Path]::GetTempPath()) "$([guid]::NewGuid()).json"

function Assert-Condition {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw $Message
    }
}

try {
    az bicep build `
        --file (Join-Path $repoRoot 'infra\bicep\m0-private-vm.bicep') `
        --outfile $compiledFile `
        --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to compile the isolated M0 VM template.'
    }

    $template = Get-Content -LiteralPath $compiledFile -Raw | ConvertFrom-Json
    $expectedTypes = @(
        'Microsoft.Compute/virtualMachines'
        'Microsoft.Network/networkInterfaces'
        'Microsoft.Network/networkSecurityGroups'
        'Microsoft.Network/publicIPAddresses'
        'Microsoft.Network/virtualNetworks'
    )
    $actualTypes = @($template.resources.type | Sort-Object)
    Assert-Condition (($actualTypes -join ',') -eq ($expectedTypes -join ',')) `
        'M0 must create only one VM and its dedicated network; no baseline services.'
    Assert-Condition ($template.parameters.sshPublicKey.type -eq 'secureString') `
        'The SSH key input must not be emitted in deployment history.'
    Assert-Condition (($template.parameters.vmSize.allowedValues -join ',') -eq
        'Standard_D4as_v6,Standard_D8as_v6') 'Use standard-memory v6 compute rather than small burstable SKUs.'
    Assert-Condition (-not $template.parameters.sshSourceIpv4.PSObject.Properties['defaultValue']) `
        'An operator IPv4 must be supplied explicitly.'
    Assert-Condition (($template.parameters.sshPort.allowedValues -join ',') -eq '22,443' -and
        $template.parameters.sshPort.defaultValue -eq 22) 'Only SSH transport ports 22 or 443 may be selected.'
    Assert-Condition ($template.parameters.sshTlsEnabled.defaultValue -eq $false) `
        'TLS-wrapped SSH must be explicitly selected.'
    Assert-Condition ($template.variables.sshIngressPort -eq "[if(parameters('sshTlsEnabled'), 443, parameters('sshPort'))]" -and
        $template.variables.sshDaemonPort -eq "[if(parameters('sshTlsEnabled'), 22, parameters('sshPort'))]" -and
        $template.variables.sshListenAddress -eq "[if(parameters('sshTlsEnabled'), '127.0.0.1', '0.0.0.0')]") `
        'TLS transport must move plaintext SSH to loopback 22, without changing application ingress.'

    $nsg = $template.resources | Where-Object type -eq 'Microsoft.Network/networkSecurityGroups'
    $rules = @($nsg.properties.securityRules)
    Assert-Condition ($rules.Count -eq 2) 'Expected only the SSH allow and deny-all rules.'
    $allow = $rules | Where-Object name -eq 'AllowSshFromOperator'
    $deny = $rules | Where-Object name -eq 'DenyOtherInbound'
    $expectedSource = "[format('{0}/32', parameters('sshSourceIpv4'))]"
    Assert-Condition ($allow.properties.sourceAddressPrefix -eq $expectedSource) `
        'SSH must be limited to the exact operator /32.'
    Assert-Condition ($allow.properties.destinationPortRange -eq "[string(variables('sshIngressPort'))]" -and
        $allow.properties.protocol -eq 'Tcp' -and
        $allow.properties.access -eq 'Allow' -and
        $allow.properties.direction -eq 'Inbound') 'Only the selected inbound SSH TCP port may be allowed.'
    Assert-Condition ($deny.properties.access -eq 'Deny' -and
        $deny.properties.direction -eq 'Inbound' -and
        $deny.properties.protocol -eq '*' -and
        $deny.properties.sourceAddressPrefix -eq '*' -and
        $deny.properties.destinationPortRange -eq '*' -and
        $deny.properties.priority -gt $allow.properties.priority -and
        $deny.properties.priority -lt 65000) 'Other inbound traffic must be explicitly denied.'

    $vnet = $template.resources | Where-Object type -eq 'Microsoft.Network/virtualNetworks'
    Assert-Condition ($vnet.properties.subnets.Count -eq 1 -and
        $vnet.properties.subnets[0].properties.networkSecurityGroup.id -match
        'Microsoft.Network/networkSecurityGroups') 'The dedicated subnet must enforce the NSG.'
    $ip = $template.resources | Where-Object type -eq 'Microsoft.Network/publicIPAddresses'
    Assert-Condition ($ip.sku.name -eq 'Standard' -and
        $ip.properties.publicIPAllocationMethod -eq 'Static') 'Use one Standard static SSH address.'

    $vm = $template.resources | Where-Object type -eq 'Microsoft.Compute/virtualMachines'
    Assert-Condition ($vm.properties.osProfile.linuxConfiguration.disablePasswordAuthentication -eq $true -and
        -not $vm.properties.osProfile.PSObject.Properties['adminPassword']) 'VM password login must be disabled.'
    Assert-Condition ($vm.properties.osProfile.linuxConfiguration.ssh.publicKeys.Count -eq 1 -and
        $vm.properties.osProfile.linuxConfiguration.ssh.publicKeys[0].keyData -eq
        "[parameters('sshPublicKey')]") 'Use only the explicitly supplied SSH public key.'
    Assert-Condition ($vm.properties.storageProfile.imageReference.publisher -eq 'Canonical' -and
        $vm.properties.storageProfile.imageReference.offer -eq 'ubuntu-24_04-lts') 'Only the Ubuntu image is supported.'
    Assert-Condition ($vm.properties.storageProfile.osDisk.diskSizeGB -eq 64 -and
        $vm.properties.storageProfile.osDisk.managedDisk.storageAccountType -eq 'StandardSSD_LRS' -and
        $vm.properties.storageProfile.osDisk.deleteOption -eq 'Delete') 'Keep the OS disk small and removable.'
    Assert-Condition ($vm.properties.priority -eq 'Regular' -and
        $vm.properties.securityProfile.securityType -eq 'TrustedLaunch' -and
        $vm.properties.securityProfile.uefiSettings.secureBootEnabled -eq $true -and
        $vm.properties.securityProfile.uefiSettings.vTpmEnabled -eq $true) 'Require regular, Trusted Launch compute.'
    Assert-Condition (-not $vm.PSObject.Properties['identity']) 'The synthetic VM must have no Azure data-plane identity.'

    $cloudInit = Get-Content -LiteralPath (Join-Path $repoRoot 'infra\bicep\cloud-init\m0-private-vm.yaml') -Raw
    foreach ($requiredSetting in @(
        'ssh_pwauth: false', 'disable_root: true', 'Port __SDN_SSH_PORT__',
        'ListenAddress __SDN_SSH_BIND__', 'ListenStream=__SDN_SSH_BIND__:__SDN_SSH_PORT__',
        'PasswordAuthentication no',
        'KbdInteractiveAuthentication no', 'PermitRootLogin no', 'AllowUsers __SDN_ADMIN_USERNAME__',
        'AllowTcpForwarding local', 'PermitOpen 127.0.0.1:8080', 'GatewayPorts no',
        'AllowAgentForwarding no', 'X11Forwarding no', 'PermitTunnel no',
        'accept = 0.0.0.0:443', 'connect = 127.0.0.1:22', 'sslVersionMin = TLSv1.2',
        'subjectAltName=IP:$2', 'openssl verify -CAfile', 'chmod 600',
        'docker-compose-v2', 'docker compose up --detach --wait',
        'ConditionPathExists=/opt/semantic-data-nexus/compose.yaml'
    )) {
        Assert-Condition ($cloudInit.Contains($requiredSetting)) "Missing cloud-init guardrail: $requiredSetting"
    }
}
finally {
    if (Test-Path -LiteralPath $compiledFile) {
        Remove-Item -LiteralPath $compiledFile -Force
    }
}

Write-Host 'M0 VM assertions passed: minimal resources, SSH /32, deny-all ingress, key-only access, and loopback-only forwarding.'
