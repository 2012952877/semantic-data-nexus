targetScope = 'resourceGroup'

@description('Short, generic workload name used in resource names.')
@minLength(2)
@maxLength(8)
param workloadName string = 'sdn'

@description('Deployment environment.')
@allowed([
  'dev'
  'test'
  'prod'
])
param environment string

@description('Azure region for all regional resources.')
param location string = resourceGroup().location

@description('Owner or team label used for cost attribution.')
param ownerTag string = 'platform-team'

@description('Additional non-sensitive resource tags.')
param tags object = {}

@description('Deploy the optional Azure OpenAI connection boundary. No model deployment is created.')
param enableAzureOpenAI bool = false

@description('Allow public data-plane endpoints. Set false only after private endpoints, DNS, and Container Apps VNet integration exist.')
param publicNetworkAccessEnabled bool = true

@description('Log Analytics retention in days.')
@minValue(30)
@maxValue(730)
param logRetentionDays int = environment == 'prod' ? 90 : 30

@description('Daily Log Analytics ingestion cap in GB. Use -1 for no cap.')
param logDailyCapGb int = environment == 'prod' ? 5 : 1

@description('Azure AI Search SKU.')
@allowed([
  'basic'
  'standard'
  'standard2'
  'standard3'
  'storage_optimized_l1'
  'storage_optimized_l2'
])
param searchSku string = environment == 'prod' ? 'standard' : 'basic'

@description('PostgreSQL Flexible Server compute SKU.')
param postgresSkuName string = environment == 'prod' ? 'Standard_D2ds_v5' : 'Standard_B1ms'

@description('PostgreSQL Flexible Server compute tier.')
@allowed([
  'Burstable'
  'GeneralPurpose'
  'MemoryOptimized'
])
param postgresSkuTier string = environment == 'prod' ? 'GeneralPurpose' : 'Burstable'

@description('PostgreSQL storage size in GiB.')
@minValue(32)
param postgresStorageSizeGb int = environment == 'prod' ? 128 : 32

@description('PostgreSQL backup retention in days.')
@minValue(7)
@maxValue(35)
param postgresBackupRetentionDays int = environment == 'prod' ? 35 : 7

@description('Enable zone-redundant PostgreSQL high availability.')
param postgresZoneRedundant bool = environment == 'prod'

@description('Object ID of the PostgreSQL Microsoft Entra administrator. Override the synthetic example value at deployment time.')
param postgresEntraAdministratorObjectId string

@description('Display name of the PostgreSQL Microsoft Entra administrator.')
param postgresEntraAdministratorPrincipalName string

@description('Principal type of the PostgreSQL Microsoft Entra administrator.')
@allowed([
  'User'
  'Group'
  'ServicePrincipal'
])
param postgresEntraAdministratorPrincipalType string = 'Group'

@description('Container image used by the web placeholder.')
param webImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Container image used by the control API placeholder.')
param controlApiImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Container image used by the semantic API placeholder.')
param semanticApiImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Container image used by the worker job placeholder.')
param workerImage string = 'mcr.microsoft.com/azurelinux/base/core:3.0'

var uniqueSuffix = take(uniqueString(subscription().id, resourceGroup().id), 6)
var namePrefix = toLower('${workloadName}-${environment}')
var compactPrefix = replace(namePrefix, '-', '')

var names = {
  logAnalytics: '${namePrefix}-${uniqueSuffix}-law'
  appInsights: '${namePrefix}-${uniqueSuffix}-appi'
  registry: take('${compactPrefix}${uniqueSuffix}acr', 50)
  containerEnvironment: '${namePrefix}-${uniqueSuffix}-cae'
  webApp: '${namePrefix}-web'
  controlApiApp: '${namePrefix}-control-api'
  semanticApiApp: '${namePrefix}-semantic-api'
  workerJob: '${namePrefix}-worker'
  controlApiIdentity: '${namePrefix}-${uniqueSuffix}-control-mi'
  semanticApiIdentity: '${namePrefix}-${uniqueSuffix}-semantic-mi'
  workerIdentity: '${namePrefix}-${uniqueSuffix}-worker-mi'
  storage: take('${compactPrefix}${uniqueSuffix}st', 24)
  resultContainer: 'results'
  keyVault: take('${namePrefix}-${uniqueSuffix}-kv', 24)
  postgres: '${namePrefix}-${uniqueSuffix}-pg'
  postgresDatabase: 'analytics'
  search: '${namePrefix}-${uniqueSuffix}-search'
  azureOpenAI: '${namePrefix}-${uniqueSuffix}-ai'
}

var commonTags = union(tags, {
  environment: environment
  workload: workloadName
  owner: ownerTag
  managedBy: 'bicep'
  dataClassification: 'confidential'
})

module observability 'modules/observability.bicep' = {
  name: 'observability'
  params: {
    location: location
    logAnalyticsName: names.logAnalytics
    appInsightsName: names.appInsights
    retentionDays: logRetentionDays
    dailyCapGb: logDailyCapGb
    publicNetworkAccessEnabled: publicNetworkAccessEnabled
    tags: commonTags
  }
}

module identities 'modules/identities.bicep' = {
  name: 'workload-identities'
  params: {
    location: location
    controlApiIdentityName: names.controlApiIdentity
    semanticApiIdentityName: names.semanticApiIdentity
    workerIdentityName: names.workerIdentity
    tags: commonTags
  }
}

module registry 'modules/registry.bicep' = {
  name: 'container-registry'
  params: {
    location: location
    registryName: names.registry
    diagnosticWorkspaceId: resourceId('Microsoft.OperationalInsights/workspaces', names.logAnalytics)
    publicNetworkAccessEnabled: publicNetworkAccessEnabled
    tags: commonTags
  }
  dependsOn: [
    observability
  ]
}

module storage 'modules/storage.bicep' = {
  name: 'result-storage'
  params: {
    location: location
    storageAccountName: names.storage
    resultContainerName: names.resultContainer
    diagnosticWorkspaceId: resourceId('Microsoft.OperationalInsights/workspaces', names.logAnalytics)
    publicNetworkAccessEnabled: publicNetworkAccessEnabled
    tags: commonTags
  }
  dependsOn: [
    observability
  ]
}

module keyVault 'modules/key-vault.bicep' = {
  name: 'key-vault'
  params: {
    location: location
    keyVaultName: names.keyVault
    diagnosticWorkspaceId: resourceId('Microsoft.OperationalInsights/workspaces', names.logAnalytics)
    enablePurgeProtection: environment == 'prod'
    publicNetworkAccessEnabled: publicNetworkAccessEnabled
    tags: commonTags
  }
  dependsOn: [
    observability
  ]
}

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    location: location
    serverName: names.postgres
    databaseName: names.postgresDatabase
    diagnosticWorkspaceId: resourceId('Microsoft.OperationalInsights/workspaces', names.logAnalytics)
    skuName: postgresSkuName
    skuTier: postgresSkuTier
    storageSizeGb: postgresStorageSizeGb
    backupRetentionDays: postgresBackupRetentionDays
    zoneRedundant: postgresZoneRedundant
    publicNetworkAccessEnabled: publicNetworkAccessEnabled
    entraAdministratorObjectId: postgresEntraAdministratorObjectId
    entraAdministratorPrincipalName: postgresEntraAdministratorPrincipalName
    entraAdministratorPrincipalType: postgresEntraAdministratorPrincipalType
    tags: commonTags
  }
  dependsOn: [
    observability
  ]
}

module search 'modules/search.bicep' = {
  name: 'search'
  params: {
    location: location
    searchServiceName: names.search
    diagnosticWorkspaceId: resourceId('Microsoft.OperationalInsights/workspaces', names.logAnalytics)
    skuName: searchSku
    publicNetworkAccessEnabled: publicNetworkAccessEnabled
    tags: commonTags
  }
  dependsOn: [
    observability
  ]
}

module aiBoundary 'modules/ai-boundary.bicep' = {
  name: 'ai-connection-boundary'
  params: {
    location: location
    accountName: names.azureOpenAI
    diagnosticWorkspaceId: resourceId('Microsoft.OperationalInsights/workspaces', names.logAnalytics)
    enabled: enableAzureOpenAI
    publicNetworkAccessEnabled: publicNetworkAccessEnabled
    tags: commonTags
  }
  dependsOn: [
    observability
  ]
}

module containerApps 'modules/container-apps.bicep' = {
  name: 'container-apps'
  params: {
    location: location
    environmentName: names.containerEnvironment
    webAppName: names.webApp
    controlApiAppName: names.controlApiApp
    semanticApiAppName: names.semanticApiApp
    workerJobName: names.workerJob
    controlApiIdentityName: names.controlApiIdentity
    semanticApiIdentityName: names.semanticApiIdentity
    workerIdentityName: names.workerIdentity
    registryLoginServer: '${names.registry}.azurecr.io'
    diagnosticWorkspaceId: resourceId('Microsoft.OperationalInsights/workspaces', names.logAnalytics)
    webImage: webImage
    controlApiImage: controlApiImage
    semanticApiImage: semanticApiImage
    workerImage: workerImage
    minReplicas: environment == 'prod' ? 1 : 0
    tags: commonTags
  }
  dependsOn: [
    identities
    registry
    observability
  ]
}

module roleAssignments 'modules/rbac.bicep' = {
  name: 'least-privilege-rbac'
  params: {
    registryName: names.registry
    storageAccountName: names.storage
    keyVaultName: names.keyVault
    searchServiceName: names.search
    azureOpenAIAccountName: names.azureOpenAI
    enableAzureOpenAI: enableAzureOpenAI
    webAppName: names.webApp
    controlApiIdentityName: names.controlApiIdentity
    semanticApiIdentityName: names.semanticApiIdentity
    workerIdentityName: names.workerIdentity
  }
  dependsOn: [
    registry
    storage
    keyVault
    search
    aiBoundary
    containerApps
  ]
}

output resourceNames object = names
output logAnalyticsResourceId string = resourceId('Microsoft.OperationalInsights/workspaces', names.logAnalytics)
output applicationInsightsResourceId string = resourceId('Microsoft.Insights/components', names.appInsights)
output containerAppsEnvironmentResourceId string = resourceId('Microsoft.App/managedEnvironments', names.containerEnvironment)
output registryResourceId string = resourceId('Microsoft.ContainerRegistry/registries', names.registry)
output storageResourceId string = resourceId('Microsoft.Storage/storageAccounts', names.storage)
output storageBlobEndpoint string = 'https://${names.storage}.blob.${az.environment().suffixes.storage}'
output keyVaultResourceId string = resourceId('Microsoft.KeyVault/vaults', names.keyVault)
output keyVaultEndpoint string = 'https://${names.keyVault}.${az.environment().suffixes.keyvaultDns}'
output postgresResourceId string = resourceId('Microsoft.DBforPostgreSQL/flexibleServers', names.postgres)
output postgresEndpoint string = '${names.postgres}.postgres.database.azure.com'
output searchResourceId string = resourceId('Microsoft.Search/searchServices', names.search)
output searchEndpoint string = 'https://${names.search}.search.windows.net'
output azureOpenAIResourceId string = enableAzureOpenAI ? resourceId('Microsoft.CognitiveServices/accounts', names.azureOpenAI) : ''
