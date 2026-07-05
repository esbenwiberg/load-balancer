// =============================================================================
// gateway.bicep — the LiteLLM gateway container (the SUT that serves traffic).
//
// Parity note: the dev/e2e/deploy stacks run `ghcr.io/berriai/litellm:<pin>` as
// a container on :4000 with a config file mounted and secrets in env vars. The
// Azure counterpart is a Container App on a Container Apps managed environment,
// VNet-integrated into the gateway subnet, pulling its secrets from Key Vault by
// managed identity (never env-baked). Ingress (internal vs external + an IP
// allowlist) is parameterised — the actual exposure model is Needs-a-human.
//
// NOTE ON THE CONFIG FILE: the compose stacks bind-mount litellm-config.yaml.
// A Container App has no host bind-mount; the config must ship another way (baked
// into a derived image, or an Azure Files mount on the environment). That choice
// is called out in docs/11 as a follow-up; this skeleton wires the container,
// its identity, secrets, ingress, and scale — the deploy-time packaging is next.
// =============================================================================

@description('Azure region.')
param location string

@description('Resource name prefix.')
param namePrefix string

@description('Tags applied to every resource.')
param tags object = {}

@description('Resource ID of the gateway (Container Apps) subnet from network.bicep.')
param infrastructureSubnetId string

@description('Resource ID of the user-assigned managed identity the gateway runs as.')
param managedIdentityId string

@description('Resource ID of the Log Analytics workspace for the managed environment.')
param logAnalyticsWorkspaceId string

@description('LiteLLM container image. Defaults to the vetted pin (docs/03 risk 8 — never 1.82.7/1.82.8). The check.sh pin guard enforces this tag.')
param litellmImage string = 'ghcr.io/berriai/litellm:v1.83.14-stable'

@description('Container listen port (LiteLLM serves on 4000).')
param targetPort int = 4000

@description('External ingress? false = internal-only (VNet/private). Public exposure is a Needs-a-human decision; default private.')
param externalIngress bool = false

@description('CIDRs allowed to reach the gateway when ingress is enabled. Empty = allow all within the ingress scope (tighten before any public exposure).')
param allowedSourceCidrs array = []

@description('Key Vault secret URI for the LiteLLM master key.')
param masterKeySecretUri string

@description('Key Vault secret URI for DATABASE_URL.')
param databaseUrlSecretUri string

@description('Key Vault secret URI for the Foundry API key.')
param foundryApiKeySecretUri string

@description('Minimum replica count.')
param minReplicas int = 1

@description('Maximum replica count.')
param maxReplicas int = 3

// Managed environment: the VNet-integrated host for the gateway Container App.
resource managedEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-cae'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: {
      // Internal = no public static IP for the environment; ingress visibility
      // is still controlled per-app below.
      internal: true
      infrastructureSubnetId: infrastructureSubnetId
    }
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: reference(logAnalyticsWorkspaceId, '2022-10-01').customerId
        // sharedKey is resolved at deploy time from the workspace; never in git.
        sharedKey: listKeys(logAnalyticsWorkspaceId, '2022-10-01').primarySharedKey
      }
    }
  }
}

var ipRestrictions = [for cidr in allowedSourceCidrs: {
  name: 'allow-${replace(replace(cidr, '.', '-'), '/', '_')}'
  action: 'Allow'
  ipAddressRange: cidr
}]

resource gateway 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-gateway'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: managedEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: externalIngress
        targetPort: targetPort
        transport: 'auto'
        // Empty array = no IP allowlist yet. Tighten (or set external=false)
        // before any public exposure — enforced by review, not by this skeleton.
        ipSecurityRestrictions: empty(allowedSourceCidrs) ? null : ipRestrictions
      }
      // Secrets are PULLED from Key Vault by the managed identity — the values
      // never appear here or in the image. Env vars below reference them.
      secrets: [
        {
          name: 'litellm-master-key'
          keyVaultUrl: masterKeySecretUri
          identity: managedIdentityId
        }
        {
          name: 'database-url'
          keyVaultUrl: databaseUrlSecretUri
          identity: managedIdentityId
        }
        {
          name: 'foundry-api-key'
          keyVaultUrl: foundryApiKeySecretUri
          identity: managedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'litellm'
          image: litellmImage
          command: ['litellm']
          args: ['--config', '/app/config.yaml', '--port', '${targetPort}', '--num_workers', '2']
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: [
            {
              name: 'LITELLM_MASTER_KEY'
              secretRef: 'litellm-master-key'
            }
            {
              name: 'DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'FOUNDRY_API_KEY'
              secretRef: 'foundry-api-key'
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health/liveliness'
                port: targetPort
              }
              initialDelaySeconds: 20
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

@description('Container App resource ID.')
output gatewayId string = gateway.id

@description('Gateway FQDN (internal or external per ingress config).')
output gatewayFqdn string = gateway.properties.configuration.ingress.fqdn

@description('Managed environment resource ID.')
output managedEnvironmentId string = managedEnv.id
