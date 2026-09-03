param location string
param webIdentityName string
param controlApiIdentityName string
param semanticApiIdentityName string
param workerIdentityName string
param tags object

resource webIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: webIdentityName
  location: location
  tags: tags
}

resource controlApiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: controlApiIdentityName
  location: location
  tags: tags
}

resource semanticApiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: semanticApiIdentityName
  location: location
  tags: tags
}

resource workerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: workerIdentityName
  location: location
  tags: tags
}

output identityResourceIds array = [
  webIdentity.id
  controlApiIdentity.id
  semanticApiIdentity.id
  workerIdentity.id
]
output webIdentityPrincipalId string = webIdentity.properties.principalId
output controlApiIdentityPrincipalId string = controlApiIdentity.properties.principalId
output semanticApiIdentityPrincipalId string = semanticApiIdentity.properties.principalId
output workerIdentityPrincipalId string = workerIdentity.properties.principalId
