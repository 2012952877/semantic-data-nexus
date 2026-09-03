param location string
param environmentName string
param webAppName string
param controlApiAppName string
param semanticApiAppName string
param workerJobName string
param webIdentityName string
param controlApiIdentityName string
param semanticApiIdentityName string
param workerIdentityName string
param registryLoginServer string
param diagnosticWorkspaceId string
param webImage string
param controlApiImage string
param semanticApiImage string
param workerImage string
param useWorkerPlaceholderCommand bool
param minReplicas int
param tags object

var workerCommandOverride = useWorkerPlaceholderCommand ? {
  command: [
    '/bin/sh'
    '-c'
  ]
  args: [
    'echo "worker placeholder ready"'
  ]
} : {}

resource webIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: webIdentityName
}

resource controlApiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: controlApiIdentityName
}

resource semanticApiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: semanticApiIdentityName
}

resource workerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: workerIdentityName
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
    peerAuthentication: {
      mtls: {
        enabled: true
      }
    }
    peerTrafficConfiguration: {
      encryption: {
        enabled: true
      }
    }
    zoneRedundant: false
  }
}

resource webApp 'Microsoft.App/containerApps@2025-01-01' = {
  name: webAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${webIdentity.id}': {}
    }
  }
  properties: {
    environmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: true
        targetPort: 80
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
        transport: 'auto'
      }
      registries: [
        {
          identity: webIdentity.id
          server: registryLoginServer
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: webImage
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: webIdentity.properties.clientId
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: 3
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
}

resource controlApiApp 'Microsoft.App/containerApps@2025-01-01' = {
  name: controlApiAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${controlApiIdentity.id}': {}
    }
  }
  properties: {
    environmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: false
        targetPort: 80
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
        transport: 'http'
      }
      registries: [
        {
          identity: controlApiIdentity.id
          server: registryLoginServer
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'control-api'
          image: controlApiImage
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: controlApiIdentity.properties.clientId
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: 3
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
}

resource semanticApiApp 'Microsoft.App/containerApps@2025-01-01' = {
  name: semanticApiAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${semanticApiIdentity.id}': {}
    }
  }
  properties: {
    environmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: false
        targetPort: 80
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
        transport: 'http'
      }
      registries: [
        {
          identity: semanticApiIdentity.id
          server: registryLoginServer
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'semantic-api'
          image: semanticApiImage
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: semanticApiIdentity.properties.clientId
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: 5
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: '25'
              }
            }
          }
        ]
      }
    }
  }
}

resource workerJob 'Microsoft.App/jobs@2025-01-01' = {
  name: workerJobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${workerIdentity.id}': {}
    }
  }
  properties: {
    environmentId: managedEnvironment.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 1800
      replicaRetryLimit: 3
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          identity: workerIdentity.id
          server: registryLoginServer
        }
      ]
    }
    template: {
      containers: [
        union({
          name: 'worker'
          image: workerImage
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: workerIdentity.properties.clientId
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }, workerCommandOverride)
      ]
    }
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: managedEnvironment
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

output environmentName string = managedEnvironment.name
output environmentResourceId string = managedEnvironment.id
output webAppName string = webApp.name
output webEndpoint string = 'https://${webApp.properties.configuration.ingress.fqdn}'
output controlApiAppName string = controlApiApp.name
output semanticApiAppName string = semanticApiApp.name
output workerJobName string = workerJob.name
