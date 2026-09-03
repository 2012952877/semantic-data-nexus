param location string
param accountName string
param diagnosticWorkspaceId string
param enabled bool
param publicNetworkAccessEnabled bool
param tags object

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = if (enabled) {
  name: accountName
  location: location
  kind: 'OpenAI'
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: accountName
    disableLocalAuth: true
    dynamicThrottlingEnabled: true
    publicNetworkAccess: publicNetworkAccessEnabled ? 'Enabled' : 'Disabled'
    restrictOutboundNetworkAccess: true
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: publicNetworkAccessEnabled ? 'Allow' : 'Deny'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (enabled) {
  name: 'send-to-log-analytics'
  scope: account
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

output accountName string = enabled ? accountName : ''
output accountResourceId string = enabled ? resourceId('Microsoft.CognitiveServices/accounts', accountName) : ''
