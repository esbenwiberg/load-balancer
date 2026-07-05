// =============================================================================
// postgres.bicep — the persistent store (LiteLLM's virtual-key + spend ledger).
//
// Parity note: the dev/e2e stacks run `postgres:16-alpine` as an in-network
// container holding LiteLLM's virtual keys, teams, and SpendLogs (goal 11b
// proved this must survive a gateway restart). The Azure counterpart is
// PostgreSQL Flexible Server, VNet-integrated (private access) into the database
// subnet from network.bicep — the same durability guarantee, managed.
//
// The admin password is a @secure() param with a placeholder default so offline
// build/validate needs no credentials; a real deploy supplies it from a secret
// store, and the assembled DATABASE_URL lands in Key Vault (keyvault.bicep).
// =============================================================================

@description('Azure region.')
param location string

@description('Resource name prefix.')
param namePrefix string

@description('Tags applied to every resource.')
param tags object = {}

@description('Resource ID of the delegated database subnet (from network.bicep).')
param delegatedSubnetId string

@description('Private DNS zone resource ID for privatelink.postgres.database.azure.com. Empty = caller wires DNS separately.')
param privateDnsZoneId string = ''

@description('Administrator login name.')
param administratorLogin string = 'litellm'

// Required, no default — main.bicep passes it through (a @secure param must never
// carry a hardcoded default; the linter enforces it).
@description('Administrator password. Supplied by the caller; never in git.')
@secure()
param administratorPassword string

@description('Compute SKU. Burstable is the cheapest tier that fits a Phase-0 gateway store.')
param skuName string = 'Standard_B1ms'

@description('Compute tier for the SKU.')
@allowed([
  'Burstable'
  'GeneralPurpose'
  'MemoryOptimized'
])
param skuTier string = 'Burstable'

@description('Storage size in GB.')
param storageSizeGB int = 32

@description('PostgreSQL major version.')
param postgresVersion string = '16'

@description('Name of the application database created on the server.')
param databaseName string = 'litellm'

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: '${namePrefix}-pg'
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: postgresVersion
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorPassword
    storage: {
      storageSizeGB: storageSizeGB
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    // Private access: the server is reachable only inside the VNet via the
    // delegated subnet — no public endpoint. Public exposure would be a
    // Needs-a-human decision; default to private.
    network: {
      delegatedSubnetResourceId: delegatedSubnetId
      privateDnsZoneArmResourceId: empty(privateDnsZoneId) ? null : privateDnsZoneId
      publicNetworkAccess: 'Disabled'
    }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

@description('PostgreSQL server resource ID.')
output serverId string = server.id

@description('Fully-qualified domain name of the server (private).')
output serverFqdn string = server.properties.fullyQualifiedDomainName

@description('Application database name.')
output databaseName string = database.name
