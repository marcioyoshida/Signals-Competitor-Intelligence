# What we store

Where Onça's data lives, in what form, for how long. All storage is inside a single
AWS account (`us-east-1`), no third-party data processor other than AWS and the
Bedrock foundation models used for synthesis.

## Storage inventory

| Store | Contents | Where | Notes |
| --- | --- | --- | --- |
| Raw corpus | Fetched source documents/records (news metadata, regulator filings, registry snapshots) | S3 (`onca-raw-*`) | Feeds the Bedrock Knowledge Base; never article bodies (see [sources-and-licensing](sources-and-licensing.md)) |
| Knowledge Base | Vector-indexed, citable chunks of the raw corpus | S3 Vectors + Bedrock KB | Backs the grounded Q&A agent; every retrieval is traceable to a source document |
| Entities registry | Curated per-entity record: identity, aliases, industry, ownership, classification attributes, provenance | DynamoDB (`OncaEntitiesTable`) | The commercial asset (ADR 002) — the single source of truth for per-entity curation |
| Curation log | Append-only journal of every registry mutation | DynamoDB (`OncaCurationLog`) | Audit trail + field-level rollback (ADR 018) |
| Feed / synthesis output | Daily-built `feed.json` — cards, moves, alerts, framework outputs, officer dashboards | S3 (dashboard site bucket) | Derived, rebuilt daily; not a system of record |
| Tenant entitlement | `{tenant_id: tier, modules[]}` | DynamoDB (`OncaTenantConfig`) | Defines the per-tenant read boundary (ADR 016) |
| Decision log | Officer decisions captured via the Executive Flow (ADR 021) | DynamoDB (`OncaDecisionLog`) | Per-tenant; not shared across tenants |
| Engagement telemetry | Which cards/sectors a session viewed, on the shared Portal plane only | DynamoDB | Product-analytics only, never resold; absent entirely on the Sovereign/in-account plane (ADR 015) |
| Identity | Login accounts, tenant/tier claims | Amazon Cognito | `custom:tenant` is immutable once set |

## Retention

- **Raw corpus & KB:** retained for the life of the product — this is the substrate
  the corpus quality (the actual moat) is built from. No indefinite retention of
  anything not already public.
- **Curation log:** append-only, retained indefinitely — it *is* the audit trail;
  truncating it would defeat its purpose.
- **Feed output:** rebuilt daily; prior days are not specially archived beyond
  normal S3 versioning.
- **Engagement telemetry:** rolling window, product-analytics purpose only.
- Registry and entitlement tables have point-in-time recovery enabled — see
  [auth-and-isolation](auth-and-isolation.md) for the operational backup posture.

## Region & data residency

Everything lives in AWS `us-east-1`. The **Sovereign / in-account** delivery plane
(ADR 015) deploys the same stack directly into the customer's own AWS account and
region of choice — for a buyer with a data-residency requirement, this is the
plane to use, not the shared SaaS one.

## What we do not store

- No full CPF, no full personal identifying documents (see
  [lgpd-posture](lgpd-posture.md)).
- No article bodies or paywalled content, only metadata + link.
- No customer payment data (billing is handled by a separate payment processor once
  wired — Onça's own stores never touch card data).
