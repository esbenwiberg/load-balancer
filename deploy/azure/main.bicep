// =============================================================================
// main.bicep — the Azure IaC skeleton for the LLM load balancer (goal 14).
//
// Deploys the gateway and everything it directly needs, as a faithful Azure
// miniature of the dev stack (docs/11 maps every component 1:1):
//
//   dev-stack container            Azure resource (this template)
//   ---------------------------    ----------------------------------------
//   litellm gateway (:4000)     -> Container App on a managed environment
//   postgres db                 -> PostgreSQL Flexible Server (private)
//   env-var secrets             -> Key Vault secrets + managed-identity RBAC
//   docker bridge network       -> VNet + delegated subnets + NSG
//   (workbenches / Foundry)     -> EXTERNAL — not deployed here (see docs/11)
//
// SCOPE: this is CODE ONLY. It is authored to `bicep build` offline with zero
// credentials and no cloud calls (the check.sh IaC step + CI). It does NOT
// deploy — the first real deploy, exposure model, and cred wiring are all
// Needs-a-human (GOALS.md). Every secret param defaults to a non-secret
// placeholder so the template compiles and what-ifs without any real values.
// =============================================================================

targetScope = 'resourceGroup'

@description('Azure region for all resources. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Short prefix for every resource name (lowercase letters/digits/hyphens).')
@minLength(2)
@maxLength(16)
param namePrefix string = 'llmlb'

@description('Environment label applied as a tag (e.g. dev, test, prod).')
param environmentName string = 'dev'

@description('Tags applied to every resource.')
param tags object = {
  workload: 'llm-load-balancer'
  environment: environmentName
  managedBy: 'bicep'
}

// ---- secrets (required, no defaults) ----------------------------------------
// Deliberately NO defaults: a @secure param must never carry a hardcoded default
// (Bicep linter: secure-parameter-default). The example .bicepparam assigns
// obvious non-secret placeholders so offline build-params works with zero creds;
// a real deploy overrides them from a secret store / pipeline, NEVER from git.
@description('LiteLLM master key. NEVER commit a real value — supply at deploy time.')
@secure()
param litellmMasterKey string

@description('PostgreSQL administrator password. NEVER commit a real value.')
@secure()
param postgresAdminPassword string

@description('Azure AI Foundry API key. NEVER commit a real value.')
@secure()
param foundryApiKey string

// ---- networking parameters (surfaced for env-specific overrides) ------------
@description('CIDR for the whole VNet.')
param vnetAddressSpace string = '10.42.0.0/16'

@description('Expose the gateway ingress externally? Default false (private). Public exposure is a Needs-a-human decision.')
param externalIngress bool = false

@description('CIDRs allowed to reach the gateway. Empty = no allowlist yet.')
param allowedSourceCidrs array = []

// ---- gateway identity: one user-assigned MI reused for KV pull + app run ----
resource gatewayIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-gateway-id'
  location: location
  tags: tags
}

// ---- Log Analytics: the sink for the managed environment's app logs ---------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${namePrefix}-logs'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// ---- network: VNet + delegated subnets + NSG --------------------------------
module network 'modules/network.bicep' = {
  name: 'network'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    vnetAddressSpace: vnetAddressSpace
  }
}

// ---- persistent store: PostgreSQL Flexible Server (private) -----------------
module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    delegatedSubnetId: network.outputs.dbSubnetId
    administratorPassword: postgresAdminPassword
  }
}

// ---- secrets: Key Vault + managed-identity RBAC -----------------------------
// DATABASE_URL is assembled from the Postgres FQDN + admin login + the secret
// password at deploy time. Here we pass a placeholder; the deploy pipeline
// composes the real URL and sets it (never in git).
module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    gatewayPrincipalId: gatewayIdentity.properties.principalId
    litellmMasterKey: litellmMasterKey
    databaseUrl: 'postgresql://litellm@${postgres.outputs.serverFqdn}:5432/${postgres.outputs.databaseName}'
    foundryApiKey: foundryApiKey
  }
}

// ---- gateway: the LiteLLM Container App -------------------------------------
module gateway 'modules/gateway.bicep' = {
  name: 'gateway'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    infrastructureSubnetId: network.outputs.gatewaySubnetId
    managedIdentityId: gatewayIdentity.id
    logAnalyticsWorkspaceId: logAnalytics.id
    externalIngress: externalIngress
    allowedSourceCidrs: allowedSourceCidrs
    masterKeySecretUri: keyvault.outputs.masterKeySecretUri
    databaseUrlSecretUri: keyvault.outputs.databaseUrlSecretUri
    foundryApiKeySecretUri: keyvault.outputs.foundryApiKeySecretUri
  }
}

// ---- outputs ----------------------------------------------------------------
@description('Gateway FQDN (internal unless externalIngress=true).')
output gatewayFqdn string = gateway.outputs.gatewayFqdn

@description('Key Vault URI.')
output keyVaultUri string = keyvault.outputs.vaultUri

@description('PostgreSQL server FQDN (private).')
output postgresFqdn string = postgres.outputs.serverFqdn

@description('Gateway managed identity client/principal for out-of-band grants.')
output gatewayIdentityPrincipalId string = gatewayIdentity.properties.principalId
