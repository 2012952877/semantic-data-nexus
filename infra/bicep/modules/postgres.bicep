param location string
param serverName string
param databaseName string
param diagnosticWorkspaceId string
param skuName string
param skuTier string
param storageSizeGb int
param backupRetentionDays int
param zoneRedundant bool
param publicNetworkAccessEnabled bool
param entraAdministratorObjectId string
param entraAdministratorPrincipalName string
param entraAdministratorPrincipalType string
param tags object

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: '16'
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenant().tenantId
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: zoneRedundant ? 'ZoneRedundant' : 'Disabled'
    }
    network: {
      publicNetworkAccess: publicNetworkAccessEnabled ? 'Enabled' : 'Disabled'
    }
    storage: {
      autoGrow: 'Enabled'
      storageSizeGB: storageSizeGb
    }
  }
}

resource entraAdministrator 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2025-08-01' = {
  parent: server
  name: entraAdministratorObjectId
  properties: {
    principalName: entraAdministratorPrincipalName
    principalType: entraAdministratorPrincipalType
    tenantId: tenant().tenantId
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: server
  properties: {
    workspaceId: diagnosticWorkspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
  }
}

output serverName string = server.name
output serverResourceId string = server.id
output serverEndpoint string = server.properties.fullyQualifiedDomainName
output databaseName string = database.name
