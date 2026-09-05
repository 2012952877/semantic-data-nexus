targetScope = 'resourceGroup'

param location string = resourceGroup().location
param registryName string
param environmentName string
param pullIdentityName string
param appName string
param imageTag string
@description('Use a new suffix when rolling out changes to mounted configuration or secrets.')
param revisionSuffix string

@description('Bounded synthetic resolver latency. A longer test-only window makes cancellation observable over HTTPS.')
@minValue(0)
@maxValue(5000)
param fakeDelayMs int = 750

@description('One dedicated test username and salted password hash in htpasswd format. Never supply a plaintext password.')
@secure()
param authFile string

resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' existing = {
  name: registryName
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' existing = {
  name: environmentName
}

resource pullIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: pullIdentityName
}

var hostname = '${appName}.${managedEnvironment.properties.defaultDomain}'
var nginxConfiguration = replace(
  replace(loadTextContent('config/nginx.m0-aca.conf'), '__APP_HOST__', hostname),
  '__APP_HOST_REGEX__',
  replace(hostname, '.', '\\.')
)

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: location
  tags: {
    environment: 'test'
    workload: 'semantic-data-nexus'
    dataClassification: 'synthetic'
    managedBy: 'bicep'
    purpose: 'm0-authenticated-https-test'
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${pullIdentity.id}': {}
    }
  }
  properties: {
    environmentId: managedEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8080
        transport: 'http'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: pullIdentity.id
        }
      ]
      secrets: [
        {
          name: 'test-auth-file'
          value: authFile
        }
        {
          name: 'nginx-configuration'
          value: nginxConfiguration
        }
      ]
    }
    template: {
      revisionSuffix: revisionSuffix
      scale: {
        minReplicas: 0
        maxReplicas: 1
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
      volumes: [
        {
          name: 'auth'
          storageType: 'Secret'
          secrets: [
            {
              secretRef: 'test-auth-file'
              path: 'htpasswd'
            }
          ]
        }
        {
          name: 'nginx-config'
          storageType: 'Secret'
          secrets: [
            {
              secretRef: 'nginx-configuration'
              path: 'default.conf'
            }
          ]
        }
      ]
      containers: [
        {
          name: 'web'
          image: '${registry.properties.loginServer}/web:${imageTag}'
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          volumeMounts: [
            {
              volumeName: 'auth'
              mountPath: '/etc/nexus-auth'
            }
            {
              volumeName: 'nginx-config'
              mountPath: '/etc/nginx/conf.d'
            }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health/live'
                port: 8083
              }
              periodSeconds: 2
              timeoutSeconds: 2
              failureThreshold: 60
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health/live'
                port: 8083
              }
              periodSeconds: 10
              timeoutSeconds: 2
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health/ready'
                port: 8083
              }
              periodSeconds: 5
              timeoutSeconds: 5
              failureThreshold: 24
            }
          ]
        }
        {
          name: 'control-api'
          image: '${registry.properties.loginServer}/control-api:${imageTag}'
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            {
              name: 'AllowedHosts'
              value: '127.0.0.1;localhost'
            }
            {
              name: 'ASPNETCORE_ENVIRONMENT'
              value: 'Development'
            }
            {
              name: 'ASPNETCORE_URLS'
              value: 'http://127.0.0.1:8081'
            }
            {
              name: 'ASPNETCORE_HTTP_PORTS'
              value: '8081'
            }
            {
              name: 'LocalDevelopmentAuth__Enabled'
              value: 'true'
            }
            {
              name: 'OpenApi__Enabled'
              value: 'false'
            }
            {
              name: 'SemanticBackend__BaseUri'
              value: 'http://127.0.0.1:8082/'
            }
            {
              name: 'SemanticBackend__TimeoutSeconds'
              value: '15'
            }
            {
              name: 'SemanticBackend__UseFake'
              value: 'false'
            }
          ]
        }
        {
          name: 'semantic-backend'
          image: '${registry.properties.loginServer}/semantic-backend:${imageTag}'
          command: [
            'python'
            '-m'
            'uvicorn'
          ]
          args: [
            'semantic_backend.api:create_app'
            '--factory'
            '--host'
            '127.0.0.1'
            '--port'
            '8082'
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'SEMANTIC_NEXUS_RESOLVER'
              value: 'fake'
            }
            {
              name: 'SEMANTIC_NEXUS_FAKE_DELAY_MS'
              value: string(fakeDelayMs)
            }
          ]
        }
      ]
    }
  }
}

output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
