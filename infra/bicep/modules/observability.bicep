param location string
param logAnalyticsName string
param appInsightsName string
param retentionDays int
param dailyCapGb int
param publicNetworkAccessEnabled bool
param tags object

resource workspace 'Microsoft.OperationalInsights/workspaces@2025-02-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    retentionInDays: retentionDays
    publicNetworkAccessForIngestion: publicNetworkAccessEnabled ? 'Enabled' : 'Disabled'
    publicNetworkAccessForQuery: publicNetworkAccessEnabled ? 'Enabled' : 'Disabled'
    sku: {
      name: 'PerGB2018'
    }
    workspaceCapping: {
      dailyQuotaGb: dailyCapGb
    }
    features: {
      disableLocalAuth: true
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    IngestionMode: 'LogAnalytics'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: true
    publicNetworkAccessForIngestion: publicNetworkAccessEnabled ? 'Enabled' : 'Disabled'
    publicNetworkAccessForQuery: publicNetworkAccessEnabled ? 'Enabled' : 'Disabled'
  }
}

output logAnalyticsName string = workspace.name
output logAnalyticsResourceId string = workspace.id
output applicationInsightsName string = appInsights.name
output applicationInsightsResourceId string = appInsights.id
