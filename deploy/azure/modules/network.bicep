// =============================================================================
// network.bicep — the VNet the gateway lives in.
//
// Parity note: in the dev stack every service talks over a single Docker bridge
// network by service name (litellm -> db, workbench-a:9100, ...). The Azure
// counterpart is ONE VNet with purpose-built subnets: one delegated to the
// Container Apps environment (the gateway), one delegated to PostgreSQL
// Flexible Server, and one for private endpoints (Key Vault, and later Foundry).
// Everything here is PARAMETERS + address math only — no peering, no public IPs,
// no cloud calls. Deployment wiring is a Needs-a-human decision (see docs/11).
// =============================================================================

@description('Azure region for all networking resources.')
param location string

@description('Prefix applied to every resource name so multiple envs can coexist.')
param namePrefix string

@description('Tags applied to every resource.')
param tags object = {}

@description('CIDR for the whole VNet. Must be large enough for all subnets below.')
param vnetAddressSpace string = '10.42.0.0/16'

@description('Subnet CIDR delegated to the Container Apps environment (the gateway).')
param gatewaySubnetPrefix string = '10.42.0.0/23'

@description('Subnet CIDR delegated to PostgreSQL Flexible Server (the persistent store).')
param dbSubnetPrefix string = '10.42.2.0/24'

@description('Subnet CIDR for private endpoints (Key Vault today; Foundry later).')
param privateEndpointSubnetPrefix string = '10.42.3.0/24'

// NSG for the gateway subnet. Default-deny inbound from the internet; the actual
// ingress rules (who may reach :4000) are an exposure decision (Needs-a-human).
resource gatewayNsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: '${namePrefix}-gw-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'DenyAllInboundByDefault'
        properties: {
          priority: 4096
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: '${namePrefix}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [vnetAddressSpace]
    }
    subnets: [
      {
        // Delegated to the Container Apps managed environment (the gateway).
        name: 'gateway'
        properties: {
          addressPrefix: gatewaySubnetPrefix
          networkSecurityGroup: {
            id: gatewayNsg.id
          }
          delegations: [
            {
              name: 'aca-delegation'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        // Delegated to PostgreSQL Flexible Server for VNet-integrated (private) DB.
        name: 'database'
        properties: {
          addressPrefix: dbSubnetPrefix
          delegations: [
            {
              name: 'pg-delegation'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
      {
        // Private endpoints for Key Vault (and, later, Foundry) so secrets never
        // traverse the public internet.
        name: 'private-endpoints'
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

@description('Resource ID of the VNet.')
output vnetId string = vnet.id

@description('Resource ID of the gateway (Container Apps) subnet.')
output gatewaySubnetId string = vnet.properties.subnets[0].id

@description('Resource ID of the database (PostgreSQL) subnet.')
output dbSubnetId string = vnet.properties.subnets[1].id

@description('Resource ID of the private-endpoint subnet.')
output privateEndpointSubnetId string = vnet.properties.subnets[2].id
