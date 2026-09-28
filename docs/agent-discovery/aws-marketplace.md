# Onça on AWS Marketplace: Sovereign listing kit (#185, decision (a))

**Owner decision, 2026-09-28: (a).** The AWS Marketplace listing is the **Sovereign in-account
product**, placed in the *AI agents & tools* category. ADR 015/016 stand unchanged. The shared
remote MCP (`https://onssa.org/mcp`) is **not** a Marketplace product: it stays in the directories
(#183) and is billed through the Portal/SaaS rails.

## What is listed

| | |
|---|---|
| Product | Onça Sovereign: Brazilian financial competitive and regulatory intelligence, deployed in the buyer's own AWS account |
| Delivery | The tenant CDK app (`infra/tenant_app.py` / `infra/tenant_stack.py`, ADR 016 addendum), synthesized to CloudFormation. It runs in the buyer's account with telemetry **off** by construction (`infra/egress_audit.py` enforces it at synth time). Entitlement is checked only at `/resolve` (per-tenant IAM role, `src/dashboard/resolve_api.py`). |
| Agent surface in the listing copy | The in-account deployment's own `/api/ask` and, when enabled, the MCP server running **inside the buyer's account**, never the shared one |
| Price | R$ 25.000/month platform licence (ADR 024), buyer pays their own AWS bill. Sold by **private offer** (contract). |
| Category | AI agents & tools, plus a secondary Financial services / Business intelligence category |

## Listing copy (pt-BR first, EN below)

**Título:** Onça Sovereign: inteligência competitiva e regulatória do setor financeiro brasileiro, na sua conta AWS

**Resumo:** Registro curado de instituições financeiras brasileiras, sinais regulatórios com fonte
(BCB, CVM, CADE, CEIS/CNEP, PNCP, DOU) e perguntas respondidas com citação, implantados na sua
própria conta AWS. Sem telemetria para o fornecedor; a licença é verificada por uma única chamada
autenticada por IAM.

**EN summary:** A curated registry of Brazilian financial institutions, sourced regulatory signals
and cited Q&A, deployed in your own AWS account. No telemetry to the vendor; the licence is checked
by one IAM-authenticated call.

## Steps (all need the owner; none can start before 1)

1. **Blocker:** AWS Marketplace seller registration needs the legal entity (Smart Signals LLC
   amendment pending), tax interview and bank account.
2. **Delivery method:** confirm with AWS Marketplace seller operations which product type fits a
   serverless CDK stack with a contract licence. The likely fit is a *SaaS contract* listing with
   private offers, where the buyer deploys our CloudFormation template. A CloudFormation-template
   product type requires AMI or container artifacts, which Onça doesn't have. **Verify before
   building anything.**
3. **Entitlement wiring:** a Marketplace contract → `put_tenant_config(..., tier="sovereign",
   plane="marketplace")` + a `resolve_caller_role_arn` exchange (onboarding runbook §3). Reuse
   `scripts/provision_tenant.py`. There is no metering: this is a flat licence.
4. **Listing review:** the privacy and terms URLs are the same as #183 (`/docs/privacy.html`,
   `/docs/terms.html`); support is `contato@onssa.org`.

No fulfillment code is built ahead of step 2's answer.
