# What we store

Where Onça's data lives, in what form, for how long. All storage is inside a single
AWS account (`us-east-1`), no third-party data processor other than AWS and the
Bedrock foundation models used for synthesis.

## Storage inventory

| Store | Contents | Where | Retention |
| --- | --- | --- | --- |
| Raw corpus | Source documents/records (news metadata only — headline/publisher/date/link, never article body; regulator filings; registry snapshots) | S3 `onca-raw-*` | **395 days** — an explicit lifecycle rule (`infra/harden_buckets.sh`), one YoY cycle plus margin |
| Knowledge Base | Vector-indexed, citable chunks of the raw corpus | S3 Vectors + Bedrock KB | Not independently lifecycled; bounded by the raw corpus's 395-day horizon |
| Digests / narratives / feed output | Synthesized narratives, `feed.json`, framework outputs | S3 `onca-digests-*` | **No expiration, by design** — explicitly excluded from the raw-bucket lifecycle rule; this is the analytical memory |
| Entities registry | Curated per-entity record: identity, aliases, industry, ownership, classification, provenance | DynamoDB `OncaEntitiesTable` | No TTL. PITR + deletion protection enabled. Kept indefinitely — this is the commercial asset (ADR 002) |
| Curation log | Append-only journal of every registry mutation | DynamoDB `OncaCurationLog` | No TTL. PITR + deletion protection. Append-only, kept indefinitely — it *is* the audit trail (ADR 018) |
| Tenant entitlement | `{tenant_id: tier, modules[]}` | DynamoDB `OncaTenantConfig` | No TTL. PITR + deletion protection. Defines the per-tenant read boundary (ADR 016) |
| Decision log & engagement telemetry | Officer decisions and card-level engagement events | Items inside `OncaEntitiesTable` — **not** separate tables | No TTL; inherits the entities table's retention. See gap below on tenant scoping |
| Dashboard site | Built static site + `feed.json` copies | S3 (CloudFront origin) | Rebuilt on every deploy, public-access-blocked, TLS-enforced |
| Identity | Login accounts, `custom:tenant`/`custom:tier` claims | Amazon Cognito | Retained across stack updates. No password ever touches Onça code — Cognito Hosted-UI/PKCE |

## Backup / recovery posture

Point-in-time recovery (PITR) + deletion protection are enabled on the three tables
whose loss would be unrecoverable through application logic alone: the entities
registry, tenant entitlement, and curation log. A real restore drill (not just "PITR:
enabled") was run and recorded in [backup-restore-drill.md](backup-restore-drill.md):
a live restore completed in ~4.5 minutes with a verified exact key-set match. The
ingest dedup/state table has no PITR — it holds only recomputable operational state,
not customer data.

## Region & data residency

Everything lives in AWS `us-east-1`. The **Sovereign / in-account** delivery plane
deploys the same stack directly into the customer's own AWS account and region of
choice — for a buyer with a data-residency requirement, this is the plane to use, not
the shared SaaS one.

## What we do not store

- No full article bodies — metadata + link only (see
  [sources-and-licensing](sources-and-licensing.md)).
- No full CPF, no full personal identifying documents — see
  [lgpd-posture](lgpd-posture.md).
- No customer payment data — Onça's own stores never touch card data.
- No passwords — authentication is Cognito Hosted-UI/PKCE.

## Honest gaps — do not paper over these on the call

1. **No automated retention/deletion policy on the DynamoDB stores.** No TTL
   attribute is set on any table; the entities registry, curation log, tenant
   config, decision log, and engagement telemetry are all retained indefinitely by
   default, not by a stated policy. Asked "how is my data purged on request," the
   honest answer today is: there is no automated purge; it would be a manual
   delete.
2. **Decision log and engagement telemetry are not separate, more restrictively
   controlled tables** — they are items co-located in `OncaEntitiesTable`, and
   neither writes a `tenant_id` field; isolation for this data relies on the same
   shared read-boundary pattern as the rest of the SaaS plane, not a stored
   per-row tenant key.
3. **Access-log buckets (CloudFront + S3 server access logs) have no lifecycle
   rule** — they accumulate indefinitely, unlike the raw corpus's deliberate
   395-day expiry.

## Verify this yourself

- `grep -n "s3.Bucket(\|dynamodb.Table(\|point_in_time_recovery\|deletion_protection" infra/app.py`
- `infra/harden_buckets.sh` — the raw-bucket 395-day lifecycle rule.
- `src/synth/decision_log.py`, `src/synth/engagement_log.py` — storage location and lack of a `tenant_id`/TTL.
- [auth-and-isolation](auth-and-isolation.md) — the operational backup posture and the read boundary.
