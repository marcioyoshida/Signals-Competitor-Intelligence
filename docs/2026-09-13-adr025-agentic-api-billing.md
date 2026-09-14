# ADR 025 — Agentic API billing (pilot fork: Onça)

- Status: **Proposed** — 2026-09-13.
- Canonical design (shared across all six Signals forks) lives in the
  Storefront repo:
  [`docs/adr-agentic-api-billing.md`](../../storefront/docs/adr-agentic-api-billing.md).
  This file records only the Onça-specific pilot notes and the relationship to
  ADR 024.

## Relationship to ADR 024

[ADR 024's pricing amendment](2026-09-12-adr024-pricing.md), signed off the same
day as this ADR, rejected usage-based pricing for Onça's core SaaS/dashboard
product, citing a standing decision to hold metering work. **This ADR is a
deliberate, owner-confirmed exception**, not a reversal: the hold was about
repricing the existing per-module dashboard product (habit-forming weekly-brief
value, not transactions); the Agentic API is a new product surface — a
tenant's own agents/automation calling Onça programmatically — aimed at a
different buyer and priced on a different basis (Bedrock token cost, not
seats/modules). The two decisions coexist: dashboard access stays flat
per-module; API access, where purchased, is metered.

## Onça-specific pilot notes

- Reuse the existing `tenant_config` table (`src/dashboard/tenant_config.py`)
  for the new `APIKEY#<key_id>` item type — no new table.
- New machine route `POST /api/v1/agent/ask`, wrapping the same grounded-RAG
  pipeline `src/dashboard/agent_ask.py` already serves to the human
  Cognito-JWT `/api/ask` path (ADR 010) — API-key auth is an additional mode
  on the same pipeline, not a fork of it.
- Admin surface: a new tab under `/exec` (the officer dashboard, ADR-021),
  gated the same way the existing write-agent (`/api/act`, ADR-020) authz is —
  reuse that authz check rather than inventing a second one.
- Included token quota only applies to tenants that have purchased a SaaS
  module with API access included (per the Storefront ADR's pricing shape) —
  Entry-tier tenants get no API access, consistent with ADR 024/015's
  near-zero-marginal-cost design point for that plane.
**SHIPPED + LIVE-VERIFIED 2026-09-14.** `src/dashboard/api_keys.py` (hashed
key store, `onca-api-keys` + `tenant-index` GSI), `api_usage.py`
(`onca-api-usage`, atomic per-cycle token counters), `agent_api.py`
(`POST /api/v1/agent/ask`, API-key auth, reuses `agent_ask.answer`),
`api_keys_api.py` (self-service `GET/POST /api/keys` +
`POST /api/keys/revoke`, JWT-gated, tenant from the verified identity only).
New panel in `site/v2/app/index.html` (list/create/revoke, usage-this-cycle,
secret shown once). `bedrock_llm.converse()` gained an optional `usage_out`
param. Deployed via `OncaPrototypeStack`. Live end-to-end proof: created a
real key, called `/api/v1/agent/ask` through CloudFront and got a grounded
answer with citations, confirmed `onca-api-usage` recorded 10,152 tokens and
the key's `last_used_at` updated, revoked the key and confirmed the next
call 401s. 22 new tests, 1183 total green.

Deploy gotcha hit and fixed: this repo's Lambda asset is staged separately
(`build/lambda/`, populated by `rsync -a --delete src/ build/lambda/src/`
per `buildspec.yml`) — a `cdk deploy` alone does NOT pick up new `src/`
files; the first deploy attempt 500'd with `ImportModuleError` until the
staging step ran. Reusable lesson for any future direct (non-CI) deploy of
this stack.

Not yet built (follow-ups, not blocking the pilot): Storefront's Stripe
metered line items + usage-reporting job (the ADR's still-open sub-decision
on shared-table vs. poll-endpoint), and replicating this pattern to the
other five forks.
