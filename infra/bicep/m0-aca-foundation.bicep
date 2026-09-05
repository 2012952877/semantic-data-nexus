targetScope = 'resourceGroup'

param location string = resourceGroup().location
param registryName string
param environmentName string
param pullIdentityName string

var tags = {
  environment: 'test'
  workload: 'semantic-data-nexus'
  dataClassification: 'synthetic'
  managedBy: 'bicep'
  purpose: 'm0-authenticated-https-test'
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource pullIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: pullIdentityName
  location: location
  tags: tags
}

module pull 'modules/registry-pull.bicep' = {
  name: 'm0-registry-pull'
  params: {
    registryName: registry.name
    principalId: pullIdentity.properties.principalId
  }
  dependsOn: [
    registry
  ]
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
  }
}

output registryLoginServer string = registry.properties.loginServer
output environmentDomain string = managedEnvironment.properties.defaultDomain
output pullIdentityResourceId string = pullIdentity.id
