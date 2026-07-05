# 11 — Azure IaC skeleton & local↔cloud parity (goal 14)

> **Status:** code-only skeleton. It compiles offline (`bicep build`) with zero
> credentials and **does not deploy**. The first real deploy, the exposure model,
> and credential wiring are all *Needs-a-human* (see [GOALS.md](../GOALS.md)).

The balancer must end up Azure-hosted. This doc pins the Infrastructure-as-Code
that describes that host, records the reversible tooling decision, and — most
importantly — maps **every dev-stack component to its Azure counterpart** so the
local stack stays a faithful miniature of production. The IaC lives in
[`deploy/azure/`](../deploy/azure/); its own README covers how to build/validate.

## Decision: Bicep, not Terraform (reversible)

Goal 14 explicitly leaves the tool choice open ("bicep or terraform — a
reversible call: decide it and document the reasons"). **We chose Bicep.**

| Criterion | Bicep | Terraform | Why it tips |
|---|---|---|---|
| **Offline validation** | `bicep build` transpiles to ARM JSON with **no network at all** — no init, no provider download, no state. | `terraform validate` requires `terraform init` first, which **pulls the `azurerm` provider from the registry** (a network fetch). | Goal 14's hard constraint is *offline tooling only, no cloud calls*. Bicep meets it with nothing installed but the compiler; Terraform needs a registry round-trip before it can even validate. |
| **Cloud footprint** | Single-cloud, Azure-native (first-party). | Multi-cloud. | We are Azure-committed (north star). Multi-cloud portability is a cost we don't need to pay. |
| **State** | Stateless (ARM/deployment-stack handles drift). | State file to store, lock, and secure. | One less thing to secure and back up for a small gateway deployment. |
| **CI cost** | One binary (`az bicep install`), no per-run provider cache. | Provider download + state backend config per run. | Simpler, faster, more deterministic CI. |
| **Ecosystem fit** | `az`/managed identity/Container Apps are described in first-party types kept current by Microsoft. | Provider lags the ARM API occasionally. | Newest Azure resource shapes land in Bicep first. |

**Why this is reversible:** the resource topology (VNet + subnets, Key Vault +
RBAC, PostgreSQL Flexible Server, Container App) is tool-agnostic. Porting to
Terraform later is a mechanical re-expression of the same graph, not a redesign —
so committing to Bicep now costs us nothing if the multi-cloud need ever appears.

**Trade-off we accept:** if the org later standardises on Terraform for *all*
infra, we'd rewrite these ~5 files. Cheap insurance vs. carrying Terraform's
init/state/provider weight for an Azure-only, single-service deployment today.

## What the skeleton describes

[`deploy/azure/main.bicep`](../deploy/azure/main.bicep) orchestrates four modules
(each a resource group–scoped module under `modules/`):

- **`network.bicep`** — one VNet with three purpose-built subnets: `gateway`
  (delegated to the Container Apps environment), `database` (delegated to
  PostgreSQL Flexible Server), and `private-endpoints` (Key Vault today, Foundry
  later). A default-deny NSG on the gateway subnet. All addressing is parameters.
- **`postgres.bicep`** — PostgreSQL Flexible Server, VNet-integrated (private, no
  public endpoint), holding LiteLLM's virtual-key + spend ledger.
- **`keyvault.bicep`** — Key Vault (RBAC-auth, soft-delete + purge protection),
  three secret placeholders, and a *Key Vault Secrets User* role assignment for
  the gateway's managed identity.
- **`gateway.bicep`** — the LiteLLM Container App on a VNet-integrated managed
  environment, running as the user-assigned managed identity, pulling every
  secret from Key Vault by reference (never env-baked), with parameterised
  ingress (internal vs external + IP allowlist).

Secrets are **required `@secure()` params with no defaults** — a hardcoded secure
default is a Bicep anti-pattern (linter: `secure-parameter-default`). The example
[`main.example.bicepparam`](../deploy/azure/main.example.bicepparam) assigns
obvious non-secret placeholders so offline `build-params` needs zero credentials;
a real deploy overrides them from a secret store / pipeline, never from git.

## Parity: every dev-stack component → its Azure counterpart

The dev stack ([`e2e/docker-compose.dev.yaml`](../e2e/docker-compose.dev.yaml))
is the contract the cloud must honour. Each row maps a dev component to what runs
it in Azure, and whether *this* IaC skeleton provisions it.

| Dev-stack component | Role locally | Azure counterpart | In this IaC? |
|---|---|---|---|
| `litellm` gateway (`:4000`) | The SUT — the balancer | **Container App** on a VNet-integrated managed environment, user-assigned MI | ✅ `gateway.bicep` |
| `db` (`postgres:16-alpine`) | LiteLLM key/team/spend store | **PostgreSQL Flexible Server**, private (VNet-integrated) | ✅ `postgres.bicep` |
| env-var secrets (`LITELLM_MASTER_KEY`, `DATABASE_URL`, Foundry key) | Plain env (fine for keyless mocks) | **Key Vault secrets** pulled by managed identity via `secretRef` | ✅ `keyvault.bicep` + `gateway.bicep` |
| Docker bridge network + service-name DNS (`litellm→db`, `workbench-a:9100`) | In-network reachability | **VNet + delegated subnets + NSG** (+ private DNS for Key Vault/Postgres) | ✅ `network.bicep` |
| Host-published ports (`4000:4000`, …) | Reach the gateway from the host | **Container App ingress** (`externalIngress` + `allowedSourceCidrs` params) | ✅ `gateway.bicep` (params only; exposure = Needs-a-human) |
| stdout `ROUTING_RECORD` / app logs | Observability sink | **Log Analytics workspace** wired to the managed environment | ✅ `main.bicep` (workspace) |
| `workbench-a` / `workbench-b` (`mockd`) | Mock Spark workbenches | **Real Spark workbenches** — self-hosted, reached over private link | ❌ external; not provisioned here (Spark intake is parked — GOALS.md) |
| `foundry` (`mockd`) | Mock always-up tier | **Azure AI Foundry** — a managed service with its own endpoint | ❌ external; its key lands in Key Vault, endpoint in gateway config |
| `dashboard` (`:9300`) | "Where did my prompt go?" viewer | **Container App** (co-deploy) *or* a sidecar container | ⏳ follow-up (noted below) |
| `control-plane` (`:9400`) | Fleet registry + heartbeat | **Container App** (co-deploy), state store TBD | ⏳ follow-up (noted below) |
| Bind-mounted config (`litellm-config.yaml`, `obs_callback.py`) | Mounted into the container | **Baked into a derived image** *or* an **Azure Files** mount on the environment | ⏳ follow-up (noted below) |

### Deliberate gaps (called out, not hidden)

A skeleton that silently omitted things would read as "done" when it isn't. The
gaps below are intentional — each is either external, parked, or a follow-up:

1. **Config delivery.** A Container App has no host bind-mount. The dev stack
   mounts `litellm-config.yaml` + `obs_callback.py`; in Azure they must ship via
   a derived image or an Azure Files volume. The skeleton wires the container,
   identity, secrets, ingress, and scale — the packaging choice is the next PR.
2. **Dashboard + control-plane.** Both are stdlib services; in Azure each becomes
   its own Container App (or a sidecar). Left out of the first skeleton to keep it
   focused on the gateway + its direct dependencies (the goal-14 scope). Their
   parity rows above pin the intended shape.
3. **Workbenches & Foundry are external.** Sparks are self-hosted boxes (intake
   parked); Foundry is a managed Azure service. This IaC provisions the *host for
   the balancer*, not the backends it fronts — it wires the Foundry key into Key
   Vault and leaves the endpoint to gateway config.
4. **DNS + private endpoints.** The subnets and a `privateDnsZoneId` param exist,
   but the private DNS zones and private-endpoint resources for Key Vault/Foundry
   are a deploy-time wiring step (they need the target resource IDs), left to the
   first real deploy.

## The offline gate

`scripts/check.sh` (fast tier, so it runs in pre-commit **and** CI) builds every
`.bicep` and `.bicepparam` to stdout and **fails on any diagnostic** — not just
errors, so the IaC stays lint-clean (no accidental secure-defaults or unused
params). It makes **no cloud calls, needs no credentials, and never deploys**;
`bicep build` is a pure local transpile. CI installs bicep via `az bicep install`
(a tool download from GitHub releases — not an Azure API call). The litellm
image-pin guard (docs/03 risk 8) was extended to cover `.bicep`, so the gateway
image default can't drift from the vetted tag either.

## When this becomes deployable (Needs-a-human)

Per [GOALS.md](../GOALS.md), the first Azure deploy needs: a subscription +
resource-group choice, the exposure model (private endpoint vs public + IP
allowlist + TLS + dashboard auth), how the master key and Foundry creds are
minted/rotated, and DISCO data-governance sign-off for any real Foundry traffic.
Until then this stays code-only, and CLAUDE.md's auto-merge tripwire ("the moment
a real gateway serves traffic off `main`") remains un-tripped — nothing here
deploys.
