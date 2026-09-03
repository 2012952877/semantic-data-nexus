using '../main.bicep'

param workloadName = 'sdn'
param environment = 'dev'
param location = 'eastus2'
param ownerTag = 'platform-team'
param enableAzureOpenAI = false
param publicNetworkAccessEnabled = true
param searchSku = 'basic'
param postgresSkuName = 'Standard_B1ms'
param postgresSkuTier = 'Burstable'
param postgresStorageSizeGb = 32
param postgresBackupRetentionDays = 7
param postgresZoneRedundant = false
param postgresAllowedIpAddresses = []
param postgresEntraAdministratorObjectId = '00000000-0000-0000-0000-000000000000'
param postgresEntraAdministratorPrincipalName = 'replace-with-entra-admin-group'
param postgresEntraAdministratorPrincipalType = 'Group'
param tags = {
  costCenter: 'engineering'
  purpose: 'clean-room-analytics'
}
