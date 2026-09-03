param location string
param searchServiceName string
param diagnosticWorkspaceId string
param skuName string
param publicNetworkAccessEnabled bool
param tags object

resource searchService 'Microsoft.Search/searchServices@2025-05-01' = {
  name: searchServiceName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: skuName
  }
  properties: {
    disableLocalAuth: true
    hostingMode: 'Default'
    networkRuleSet: {
      bypass: 'AzureServices'
      ipRules: []
    }
    partitionCount: 1
    publicNetworkAccess: publicNetworkAccessEnabled ? 'enabled' : 'disabled'
    replicaCount: 1
    semanticSearch: 'disabled'
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: searchService
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

output searchServiceName string = searchService.name
output searchServiceResourceId string = searchService.id
output searchEndpoint string = searchService.properties.endpoint
