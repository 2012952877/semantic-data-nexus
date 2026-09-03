using '../main.bicep'

param workloadName = 'sdn'
param environment = 'prod'
param location = 'eastus2'
param ownerTag = 'platform-team'
param enableAzureOpenAI = false
param publicNetworkAccessEnabled = true
param logRetentionDays = 90
param logDailyCapGb = 5
param searchSku = 'standard'
param postgresSkuName = 'Standard_D2ds_v5'
param postgresSkuTier = 'GeneralPurpose'
param postgresStorageSizeGb = 128
param postgresBackupRetentionDays = 35
param postgresZoneRedundant = true
param postgresAllowedIpAddresses = []
param postgresEntraAdministratorObjectId = '00000000-0000-0000-0000-000000000000'
param postgresEntraAdministratorPrincipalName = 'replace-with-entra-admin-group'
param postgresEntraAdministratorPrincipalType = 'Group'
param tags = {
  costCenter: 'engineering'
  purpose: 'clean-room-analytics'
  criticality: 'high'
}
