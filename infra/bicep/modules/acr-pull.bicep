param registryName string
param webIdentityPrincipalId string
param controlApiIdentityPrincipalId string
param semanticApiIdentityPrincipalId string
param workerIdentityPrincipalId string

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' existing = {
  name: registryName
}

resource webAcrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, webIdentityPrincipalId, acrPullRoleId)
  scope: registry
  properties: {
    principalId: webIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource controlApiAcrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, controlApiIdentityPrincipalId, acrPullRoleId)
  scope: registry
  properties: {
    principalId: controlApiIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource semanticApiAcrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, semanticApiIdentityPrincipalId, acrPullRoleId)
  scope: registry
  properties: {
    principalId: semanticApiIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource workerAcrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, workerIdentityPrincipalId, acrPullRoleId)
  scope: registry
  properties: {
    principalId: workerIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}
