param location string
param controlApiIdentityName string
param semanticApiIdentityName string
param workerIdentityName string
param tags object

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
  controlApiIdentity.id
  semanticApiIdentity.id
  workerIdentity.id
]
