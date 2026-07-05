# Azure IaC skeleton (goal 14) — code only, no deploy

Infrastructure-as-Code describing the Azure host for the LLM load balancer: the
gateway container, its persistent store, Key Vault secret wiring, and networking.
This is a **skeleton**: it compiles offline with zero credentials and **does not
deploy**. The first real deploy is *Needs-a-human* — see [`../../GOALS.md`](../../GOALS.md)
and the parity + decision write-up in [`docs/11`](../../docs/11-azure-iac.md).

## Why Bicep (not Terraform)

Reversible call, made deliberately: Bicep's `build` validates **fully offline**
(no `init`, no provider download, no state), it's Azure-native (matching the
north star), and it's stateless — the cheapest fit for an Azure-only, single-
service deployment. Terraform's `validate` needs `init` (a provider registry
fetch), which would violate goal 14's "offline tooling only" constraint. Full
reasoning + the reversibility argument: [`docs/11`](../../docs/11-azure-iac.md).

## Layout

```
deploy/azure/
├── main.bicep                  orchestrator: identity, Log Analytics, module wiring
├── main.example.bicepparam     example params (commit-safe placeholders, no secrets)
└── modules/
    ├── network.bicep           VNet + delegated subnets (gateway/db/PE) + NSG
    ├── postgres.bicep          PostgreSQL Flexible Server (private) — persistent store
    ├── keyvault.bicep          Key Vault + secret placeholders + MI role assignment
    └── gateway.bicep           LiteLLM Container App (MI, KV-referenced secrets, ingress)
```

## Validate offline (no cloud calls, no credentials)

This is exactly what `scripts/check.sh` (fast tier) and CI run:

```bash
# transpile the whole module tree to ARM JSON (discard output) — pure local
az bicep build --file main.bicep --stdout > /dev/null
# validate the example parameter file against the template
az bicep build-params --file main.example.bicepparam --stdout > /dev/null
```

Standalone `bicep` works too (positional syntax): `bicep build main.bicep --stdout`.
Both are **transpile-only** — no `az login`, no subscription, no network to Azure.
The build must be **diagnostic-clean**; check.sh fails the gate on any warning.

## Secrets

The three `@secure()` params (`litellmMasterKey`, `postgresAdminPassword`,
`foundryApiKey`) have **no defaults** (a hardcoded secure default is a Bicep
anti-pattern). `main.example.bicepparam` assigns obvious non-secret placeholders
so offline `build-params` needs zero creds. A real deploy overrides them from a
secret store / pipeline — **never** by committing real values here (CLAUDE.md).
Deploying the example as-is fails loudly (the gateway rejects the placeholder
master key), which is intended.

## What this does NOT do

- **Deploy.** No `az deployment ... create` anywhere. Code + offline validation only.
- **Provision the backends.** Workbenches (self-hosted Sparks) and Foundry (a
  managed Azure service) are external; this provisions the *host for the balancer*.
- **Ship the config file or the dashboard/control-plane.** Called out as
  follow-ups in [`docs/11`](../../docs/11-azure-iac.md#deliberate-gaps-called-out-not-hidden).
- **Decide exposure.** `externalIngress`/`allowedSourceCidrs` are parameters,
  defaulting to private. The exposure model is a Needs-a-human decision.
