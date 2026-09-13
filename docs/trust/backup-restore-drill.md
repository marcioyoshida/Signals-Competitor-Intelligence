# Registry backup & restore — drill record

Closes #110/G2. Point-in-time recovery (PITR) and deletion protection are enabled
on the three tables whose loss would be unrecoverable through application logic
alone (`infra/app.py`):

- `OncaEntitiesTable` — the curated entities registry, the commercial asset
- `OncaTenantConfig` — per-tenant entitlement (loss = every tenant locked out)
- `OncaCurationLog` — the append-only audit/rollback journal (ADR 018)

ADR 018 gives field-level rollback over the curation journal; it does not survive
loss of the table itself. PITR + a rehearsed restore is the control that does.

## Drill (2026-09-12, `OncaTenantConfig`)

Restored the live `OncaTenantConfig` table to a scratch table via PITR, to prove
the mechanism works end-to-end rather than trusting that "PITR: ENABLED" alone
means a restore will succeed.

```
aws dynamodb restore-table-to-point-in-time \
  --source-table-name OncaPrototypeStack-OncaTenantConfigCADB28F8-IM6BJDHGUAPH \
  --target-table-name onca-restore-drill-tenantconfig-<ts> \
  --use-latest-restorable-time
```

- **Start (restore API call issued):** 2026-09-12 21:38:04 -03
- **Table reached `ACTIVE`:** 2026-09-12 21:42:38 -03
- **Observed RTO: ~4.5 minutes (267s)** for a 7-item table. DynamoDB
  `restore-table-to-point-in-time` time is not strongly item-count-sensitive at
  this table's size (single-digit-KB), so this figure is representative for
  `OncaTenantConfig` and `OncaCurationLog` at current scale; `OncaEntitiesTable`
  (114 entities, larger item bodies) should be re-timed once it grows
  meaningfully, since DynamoDB restore time does scale with table size at larger
  volumes.
- **Integrity check:** scanned both tables and confirmed the identical set of 7
  `tenant_id` keys (`acquiring, fintech, insurance, onca-entry-pilot,
  onca-mkt-pilot, onca-saas-pilot, wealth-management`) — not just an item count
  match, an exact key-set match.
- **Cleanup:** scratch table deleted immediately after verification — this was a
  rehearsal, not a real recovery event, and PITR restores always land in a new
  table name (never overwrite the source), so there was no risk to production
  during the drill.

## What this proves, and what it doesn't

- Proves: the restore mechanism itself works, produces a byte-for-byte-identical
  table, and completes in single-digit minutes for tables at today's scale.
- Does not prove: recovery of `OncaEntitiesTable` at its current size (should be
  re-timed directly rather than extrapolated, next time this drill runs), or a
  recovery *procedure* beyond the raw API call (e.g. re-pointing Lambda env vars
  at a restored table name, which was out of scope for this drill and would be
  the next step in a real incident).

## Recovery runbook (for a real incident)

1. Identify the last-good point in time (`describe-continuous-backups` gives the
   restorable window — currently 35 days).
2. `restore-table-to-point-in-time` into a new table name (never the source name
   — DynamoDB does not support in-place PITR restore).
3. Verify the restored data (key-set/spot-check, as above).
4. Update the affected Lambda(s)' `ONCA_ENTITIES_TABLE` / tenant-config table env
   var to the restored table name (CDK deploy or direct
   `aws lambda update-function-configuration`), or rename tables via a follow-up
   CDK deploy if a permanent swap is needed.
5. Do not delete the damaged original table until the restored replacement has
   been running cleanly — it remains the forensic record of what went wrong.
