targetScope = 'resourceGroup'

@description('Deploy only into a new resource group dedicated to the synthetic M0 test.')
param location string = resourceGroup().location

@description('Generic prefix for this isolated test environment.')
@minLength(2)
@maxLength(24)
param namePrefix string = 'sdn-m0-test'

@description('Dedicated Linux SSH administrator name, supplied only at deployment time.')
@minLength(1)
@maxLength(32)
param adminUsername string

@description('Select an available standard-memory v6 Linux SKU after checking regional availability and current pricing.')
@allowed([
  'Standard_D4as_v6'
  'Standard_D8as_v6'
])
param vmSize string

@description('One operator public IPv4 address, without a CIDR suffix. Only this /32 may connect to SSH.')
@minLength(7)
@maxLength(15)
param sshSourceIpv4 string

@description('SSH transport port, not an HTTP/HTTPS application listener. Use 443 only when the operator network requires it.')
@allowed([
  22
  443
])
param sshPort int = 22

@description('Wrap SSH in system stunnel on TCP 443. SSH then listens only on VM loopback port 22; this never exposes the web application.')
param sshTlsEnabled bool = false

@description('A dedicated SSH public key. Never supply a private key or password.')
@secure()
@minLength(200)
param sshPublicKey string

@description('Canonical Ubuntu 24.04 image version. Resolve and pin the version at deployment time.')
param imageVersion string

@description('Additional non-sensitive cost/ownership tags.')
param tags object = {}

var commonTags = union(tags, {
  environment: 'test'
  workload: 'semantic-data-nexus'
  dataClassification: 'synthetic'
  managedBy: 'bicep'
  purpose: 'm0-ssh-test'
})
var sshIngressPort = sshTlsEnabled ? 443 : sshPort
var sshDaemonPort = sshTlsEnabled ? 22 : sshPort
var sshListenAddress = sshTlsEnabled ? '127.0.0.1' : '0.0.0.0'
var sshCloudInit = replace(
  replace(loadTextContent('cloud-init/m0-private-vm.yaml'), '__SDN_ADMIN_USERNAME__', adminUsername),
  '__SDN_SSH_PORT__',
  string(sshDaemonPort)
)
var cloudInit = replace(
  replace(
    replace(sshCloudInit, '__SDN_SSH_BIND__', sshListenAddress),
    '__SDN_TLS_ENABLED__',
    string(sshTlsEnabled)
  ),
  '__SDN_TLS_SERVER_IP__',
  publicIp.properties.ipAddress
)

resource networkSecurityGroup 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: '${namePrefix}-nsg'
  location: location
  tags: commonTags
  properties: {
    securityRules: [
      {
        name: 'AllowSshFromOperator'
        properties: {
          priority: 100
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: '${sshSourceIpv4}/32'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: string(sshIngressPort)
        }
      }
      {
        name: 'DenyOtherInbound'
        properties: {
          priority: 200
          protocol: '*'
          access: 'Deny'
          direction: 'Inbound'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: '${namePrefix}-vnet'
  location: location
  tags: commonTags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.91.0.0/24'
      ]
    }
    subnets: [
      {
        name: 'test'
        properties: {
          addressPrefix: '10.91.0.0/26'
          networkSecurityGroup: {
            id: networkSecurityGroup.id
          }
        }
      }
    ]
  }
}

resource publicIp 'Microsoft.Network/publicIPAddresses@2023-09-01' = {
  name: '${namePrefix}-ip'
  location: location
  tags: commonTags
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource networkInterface 'Microsoft.Network/networkInterfaces@2023-09-01' = {
  name: '${namePrefix}-nic'
  location: location
  tags: commonTags
  properties: {
    enableIPForwarding: false
    ipConfigurations: [
      {
        name: 'primary'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          subnet: {
            id: virtualNetwork.properties.subnets[0].id
          }
          publicIPAddress: {
            id: publicIp.id
          }
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-03-01' = {
  name: '${namePrefix}-vm'
  location: location
  tags: commonTags
  properties: {
    priority: 'Regular'
    hardwareProfile: {
      vmSize: vmSize
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: imageVersion
      }
      osDisk: {
        name: '${namePrefix}-os'
        createOption: 'FromImage'
        deleteOption: 'Delete'
        diskSizeGB: 64
        caching: 'ReadWrite'
        managedDisk: {
          storageAccountType: 'StandardSSD_LRS'
        }
      }
    }
    osProfile: {
      computerName: '${namePrefix}-vm'
      adminUsername: adminUsername
      customData: base64(cloudInit)
      linuxConfiguration: {
        disablePasswordAuthentication: true
        provisionVMAgent: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: sshPublicKey
            }
          ]
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: networkInterface.id
          properties: {
            deleteOption: 'Delete'
          }
        }
      ]
    }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: {
        secureBootEnabled: true
        vTpmEnabled: true
      }
    }
    diagnosticsProfile: {
      bootDiagnostics: {
        enabled: false
      }
    }
  }
}

output vmName string = vm.name
output sshPublicIp string = publicIp.properties.ipAddress
output sshUsername string = adminUsername
output sshTransportPort int = sshIngressPort
output sshUsesTls bool = sshTlsEnabled
output networkSecurityGroupName string = networkSecurityGroup.name
