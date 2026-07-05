// =============================================================================
// keyvault.bicep — the secret store for the gateway.
//
// Parity note: the dev/e2e stacks pass secrets as plain env vars
// (LITELLM_MASTER_KEY, DATABASE_URL, a Foundry API key) — fine for keyless mock
// backends. In Azure those MUST live in Key Vault and be pulled by the gateway's
// managed identity, never baked into the image or the container spec. This module
// creates the vault, seeds secret PLACEHOLDERS (real values are set out-of-band —
// never in git, per CLAUDE.md), and grants the gateway identity read access via
// RBAC (Key Vault Secrets User).
//
// HARD RULE: no real secret values here. The @secure() params below default to a
// non-secret placeholder so `bicep build` and offline what-if work with zero
// credentials; a real deploy supplies them from a secret store / pipeline.
// =============================================================================

@description('Azure region.')
param location string

@description('Resource name prefix.')
param namePrefix string

@description('Tags applied to every resource.')
param tags object = {}

@description('Principal (object) ID of the gateway managed identity that reads secrets.')
param gatewayPrincipalId string

// Required, no defaults — main.bicep always passes these through (a @secure param
// must never carry a hardcoded default; the linter enforces it).
@description('LiteLLM master key. Supplied by the caller; never in git.')
@secure()
param litellmMasterKey string

@description('Full DATABASE_URL for the gateway, assembled by the caller from the Postgres module + secret password.')
@secure()
param databaseUrl string

@description('Azure AI Foundry API key. Supplied by the caller; never in git.')
@secure()
param foundryApiKey string

// RBAC-authorization vault (no legacy access policies). Soft-delete + purge
// protection on: a leaked-then-deleted secret must not be silently unrecoverable.
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${namePrefix}-kv'
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    // Network exposure (public vs private-endpoint-only) is an exposure decision
    // (Needs-a-human). Default to deny-public; a private endpoint lands in the
    // private-endpoint subnet from network.bicep.
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    }
  }
}

resource secretMasterKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'litellm-master-key'
  properties: {
    value: litellmMasterKey
  }
}

resource secretDatabaseUrl 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'database-url'
  properties: {
    value: databaseUrl
  }
}

resource secretFoundryKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'foundry-api-key'
  properties: {
    value: foundryApiKey
  }
}

// Built-in role: Key Vault Secrets User (read secret values). This GUID is a
// PUBLIC, fixed Azure built-in role-definition ID (identical in every tenant) —
// not a secret. gitleaks:allow (its high entropy trips the generic-api-key rule).
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6' // gitleaks:allow

resource secretsUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, gatewayPrincipalId, keyVaultSecretsUserRoleId)
  scope: vault
  properties: {
    principalId: gatewayPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
  }
}

@description('Key Vault resource ID.')
output vaultId string = vault.id

@description('Key Vault base URI (https://<name>.vault.azure.net/).')
output vaultUri string = vault.properties.vaultUri

@description('Secret URI for the LiteLLM master key (used as a Container App secret keyVaultUrl).')
output masterKeySecretUri string = secretMasterKey.properties.secretUri

@description('Secret URI for DATABASE_URL.')
output databaseUrlSecretUri string = secretDatabaseUrl.properties.secretUri

@description('Secret URI for the Foundry API key.')
output foundryApiKeySecretUri string = secretFoundryKey.properties.secretUri
