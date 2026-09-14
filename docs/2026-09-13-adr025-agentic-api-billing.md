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
- Not started: no code yet. This ADR records the design; implementation is a
  separate pass.
