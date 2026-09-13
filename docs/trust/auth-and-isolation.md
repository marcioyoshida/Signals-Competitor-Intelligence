# Auth & isolation

How a buyer's data (and other tenants' data) is protected. The design principle is
**fail closed**: absence of a valid identity or entitlement record means no data,
never a default-open fallback.

## Authentication

- **Amazon Cognito**, PKCE Hosted-UI login — no passwords handled or stored by Onça
  code.
- Every user carries two custom claims: `custom:tenant` (**immutable** once set —
  a user cannot be silently relinked to a different tenant) and `custom:tier`
  (mutable, e.g. an entry→SaaS upgrade).
- API access is gated by a JWT authorizer on API Gateway (`/api/ask`, `/api/feed`,
  `/api/gaps`, `/api/registry`) — a request without a valid, current token is
  rejected before it reaches any handler.

## Per-tenant read boundary

- `OncaTenantConfig` (DynamoDB) is the single source of truth for what a tenant is
  entitled to see: `{tenant_id: {tier, modules[]}}`, where `modules` is the set of
  industry verticals that tenant licenses.
- `GET /api/feed` derives its response by intersecting the full corpus against the
  requesting tenant's `modules` — an unprovisioned tenant, or a tenant with an empty
  `modules` list, gets **nothing**, not a default view of everything.
- This boundary is enforced **server-side**, not by hiding UI elements — a scoped
  API response is the only thing the client ever receives; there is no
  client-trusted "which sectors am I allowed to see" flag to spoof.
- The same boundary applies to the executive officer dashboards (CSO/CRO/CCO/CPO
  views) and to the grounded Q&A agent — a tenant cannot ask a question that pulls
  answers from a vertical it does not license.

## Delivery planes and isolation strength

Three delivery planes (ADR 015/016), in increasing isolation:

1. **Entry** — shared multi-tenant portal, entry-tier verticals only, capped by an
   explicit allow-list (a not-launch-ready or above-tier industry is rejected at
   provisioning, not silently dropped).
2. **SaaS** — shared infrastructure, per-tenant scoped feed via the read boundary
   above.
3. **Sovereign / in-account (Marketplace)** — the entire stack deployed directly
   into the customer's own AWS account. No shared infrastructure at all; isolation
   is enforced by AWS account boundaries, not application logic.

## Operational backing

- The entities registry, tenant entitlement table, and curation audit log all have
  **point-in-time recovery** enabled and deletion protection turned on — the
  commercial asset has a tested restore path, not just an application-level
  rollback of individual fields.
- Pipeline failures and feed staleness are alarmed (CloudWatch) rather than failing
  silently — a buyer sees an honestly stale product, never a quietly broken one
  presented as current.
