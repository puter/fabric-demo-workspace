// Hosting for the MVOI polling service — the only non-Fabric runtime component.
//
//   az deployment group create -g rg-example -f infrastructure/main.bicep \
//     -p image=<acr>.azurecr.io/mvoi-poller:<tag> acrName=<acr> \
//        nwsUserAgent='MiamiValleyOpsIntelligence/0.1 (ops@example.com)'
//
// Normally run by the DeployPoller stage of .azure-pipelines/miami-valley.yml.
//
// Creates: Log Analytics, Container Apps environment (Consumption), a user-assigned
// managed identity, Key Vault (RBAC), Storage + Azure Files share for the SQLite
// state, one always-on Container App (0.25 vCPU / 0.5 GiB), and a scheduled job that
// resumes Eventstream destinations paused by the nightly capacity suspend.
// The job's identity also needs Contributor on the Fabric workspace (docs/deployment.md).
// Secrets are NOT set here: put `ohgo-api-key` and `eventstream-connection-string`
// in the vault afterwards (docs/deployment.md). Approx. cost: see docs/costs.md.

@description('Region for all resources')
param location string = resourceGroup().location

@description('Region for the Container Apps environment and poller app; defaults to location. Use when Container Apps has no capacity in location.')
param appLocation string = location

@description('Short name used to derive resource names')
param prefix string = 'example'

@description('Container image for the poller, e.g. <acr>.azurecr.io/mvoi-poller:<tag>')
param image string

@description('Azure Container Registry (same resource group) the image is pulled from with the managed identity; empty for a public image')
param acrName string = ''

@description('NWS User-Agent incl. contact e-mail (NWS API terms)')
param nwsUserAgent string

@description('Environment tag stamped on every event')
param mvoiEnvironment string = 'demo'

@description('Optional OneLake URL of history_baseline.json exported by nb_mvoi_gold_build')
param historyBaselineUrl string = ''

@description('UTC cron for the Eventstream resume job. Hourly through the capacity\'s active hours (it resumes at 11:00 UTC).')
param resumeStreamSchedule string = '5 11-22 * * *'

var suffix = uniqueString(resourceGroup().id)
var kvName = take('kv-${prefix}-${suffix}', 24)
var stName = take('st${prefix}${suffix}', 24)
// A distinct name per region, so moving the app does not collide with an environment
// already in the resource group (environment names are unique per resource group).
var caeName = appLocation == location ? 'cae-${prefix}-${suffix}' : 'cae-${prefix}-${uniqueString(resourceGroup().id, appLocation)}'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${prefix}-${suffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-poller'
  location: location
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = if (!empty(acrName)) {
  name: acrName
}

// AcrPull for the poller identity (no registry passwords)
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(acrName)) {
  name: guid(resourceGroup().id, acrName, identity.id, 'acrpull')
  scope: acr
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

// Key Vault Secrets User for the poller identity
resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, identity.id, 'secrets-user')
  scope: kv
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: stName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  name: '${storage.name}/default/mvoi-state'
  properties: { shareQuota: 1 }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: caeName
  location: appLocation
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'mvoistate'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: 'mvoi-state'
      accessMode: 'ReadWrite'
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-${prefix}-poller'
  location: appLocation
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [ kvSecretsUser, acrPull ]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: empty(acrName) ? [] : [
        {
          server: '${acrName}.azurecr.io'
          identity: identity.id
        }
      ]
      secrets: [
        {
          name: 'ohgo-api-key'
          keyVaultUrl: '${kv.properties.vaultUri}secrets/ohgo-api-key'
          identity: identity.id
        }
        {
          name: 'eventstream-connection-string'
          keyVaultUrl: '${kv.properties.vaultUri}secrets/eventstream-connection-string'
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'poller'
          image: image
          args: [ 'run' ]
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'OHGO_API_KEY', secretRef: 'ohgo-api-key' }
            { name: 'MVOI_EVENTSTREAM_CONNECTION_STRING', secretRef: 'eventstream-connection-string' }
            { name: 'MVOI_PUBLISHER__KIND', value: 'eventhub' }
            { name: 'MVOI_ENVIRONMENT', value: mvoiEnvironment }
            { name: 'MVOI_NWS__USER_AGENT', value: nwsUserAgent }
            { name: 'MVOI_STATE__PATH', value: '/state/mvoi_state.sqlite' }
            { name: 'MVOI_RISK__HISTORY_BASELINE_PATH', value: historyBaselineUrl }
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
          ]
          volumeMounts: [ { volumeName: 'state', mountPath: '/state' } ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8080 }
              initialDelaySeconds: 60 // Container Apps maximum
              periodSeconds: 60
              failureThreshold: 5
            }
          ]
        }
      ]
      // Exactly one replica: the poller is a singleton (one state DB, no
      // duplicate polling of rate-limited public APIs).
      scale: { minReplicas: 1, maxReplicas: 1 }
      // nobrl: SQLite's POSIX byte-range locks fail on SMB ("database is locked").
      // Locks stay local to the container, which is safe with exactly one replica.
      volumes: [ {
        name: 'state'
        storageType: 'AzureFile'
        storageName: envStorage.name
        mountOptions: 'nobrl'
      } ]
    }
  }
}

// The capacity suspends overnight and the Eventstream's Eventhouse destination stays
// Paused after it resumes. This job resumes it (backfilling from where it stopped) and
// exits in seconds when everything is already Running.
resource resumeJob 'Microsoft.App/jobs@2024-03-01' = {
  name: 'caj-${prefix}-resume-stream'
  location: appLocation
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [ acrPull ]
  properties: {
    environmentId: env.id
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: resumeStreamSchedule
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaTimeout: 2400 // longer than the command's 30 min wait for the capacity
      replicaRetryLimit: 0
      registries: empty(acrName) ? [] : [
        {
          server: '${acrName}.azurecr.io'
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'resume-stream'
          image: image
          args: [ 'resume-stream' ]
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
          ]
        }
      ]
    }
  }
}

output keyVaultName string = kv.name
output pollerIdentityPrincipalId string = identity.properties.principalId
output containerApp string = app.name
output resumeStreamJob string = resumeJob.name
