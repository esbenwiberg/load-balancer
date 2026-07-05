// =============================================================================
// main.example.bicepparam — an example parameter set for main.bicep.
//
// Copy to main.<env>.bicepparam and adjust. SECRETS ARE NOT SET HERE: the three
// @secure() params (litellmMasterKey, postgresAdminPassword, foundryApiKey) keep
// their placeholder defaults so this file is safe to commit and `bicep
// build-params` works offline with zero credentials. A real deploy supplies the
// secrets from a pipeline / secret store (e.g. `--parameters litellmMasterKey=...`
// sourced from Key Vault), NEVER from git — per CLAUDE.md.
// =============================================================================

using 'main.bicep'

param namePrefix = 'llmlb'
param environmentName = 'dev'

// SECRETS — non-secret placeholders so this file is commit-safe and offline
// `bicep build-params` works with zero credentials. A real deploy OVERRIDES these
// from a secret store / pipeline (e.g. `--parameters litellmMasterKey=@Vault...`),
// NEVER by editing real values in here. Deploying as-is fails loudly (the gateway
// rejects the placeholder master key) — which is the point.
param litellmMasterKey = 'REPLACE-AT-DEPLOY-TIME-not-a-real-secret'
param postgresAdminPassword = 'REPLACE-AT-DEPLOY-TIME-not-a-real-secret'
param foundryApiKey = 'REPLACE-AT-DEPLOY-TIME-not-a-real-secret'

// Networking — mirror the dev stack's single flat network with room to grow.
param vnetAddressSpace = '10.42.0.0/16'

// Exposure: private by default. Flipping externalIngress to true and setting an
// allowlist is a Needs-a-human exposure decision (GOALS.md / docs/11).
param externalIngress = false
param allowedSourceCidrs = []
