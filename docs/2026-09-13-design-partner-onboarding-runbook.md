# Design-partner onboarding runbook (operator path)

Closes #114. Operator-led onboarding is the accepted SaaS-plane answer for launch
(ADR 024) — this is the checklist so it's not tribal knowledge. Follow it in order;
each step names the exact command and what "correct" looks like before moving on.

Prerequisites: an AWS CLI profile with access to the account, and
`ONCA_ENTITIES_TABLE` / `ONCA_TENANT_CONFIG_TABLE` / `ONCA_CURATION_LOG_TABLE` set
(or pass `--table` explicitly to every script below — table names are in
[docs/trust/what-we-store.md](trust/what-we-store.md) or `aws dynamodb list-tables`).

## 0. Before you talk numbers: confirm the sector is sellable

Check the sector against the dated readiness table in
[ADR 024](2026-09-11-adr-launch-readiness.md#coverage-gated-ga-sector-list-2026-09-12).
**Do not onboard a tenant into `closed-pension`, `securitization`, or
`private-markets`** — they're flagged "Not ready" and excluded from sale (#119)
unless the buyer has an explicit ingestion plan (see step 3's `--force-not-ready`).
If the table is stale by the time you read this, regenerate it rather than trust it:
`scripts/curation_admin.py audit` (below) plus a look at `feed.json`'s
`industry_coverage_gaps`.

## 1. Watchlist scoping (only if the buyer names a competitor we don't track yet)

Ingestion is shared and wholesale (ADR 002) — a new tenant licensing an
already-covered vertical needs **no ingestion change**. Only touch
[`config/watchlist.yaml`](../config/watchlist.yaml) if the design partner names a
specific competitor (by CNPJ/ISPB/fund-admin substring) not already in
`competitors`/`competitor_ispb`/`juros_competitors`. This requires a pipeline
redeploy to take effect — not a per-tenant runtime setting.

## 2. Registry curation pass for the sector

Before selling a sector, confirm its curation is clean, not just populated:

```bash
python scripts/curation_admin.py audit --profile <profile>
```

- Read the report for the specific `industry` the tenant is licensing. Zero
  findings for that industry is the bar; findings elsewhere don't block this
  tenant.
- Spot-check entity resolution the way #108 was caught: open the sector's Mapa
  Competitivo (`/exec` with `?admin=1`) and confirm no competitor is drawn as two
  separate dots (a fragmented-entity bug, not a data gap).
- If a finding is real, fix it via the governed curation flow
  (`scripts/curation_admin.py rollback`/the `/v2/admin` UI) — never hand-edit the
  registry table directly; every mutation must go through `OncaCurationLog` (ADR
  018) or it has no audit trail.

## 3. Provision the tenant (entitlement + Cognito login, one command)

```bash
python scripts/provision_tenant.py put <tenant-id> saas <module1> <module2> \
  --email <buyer-contact-email> \
  --user-pool-id <pool-id-or-set-$ONCA_USER_POOL_ID> \
  --profile <profile>
```

- `<tenant-id>`: a short slug, e.g. `acme-banking`. This becomes the immutable
  `custom:tenant` Cognito claim — pick it deliberately, it cannot be changed later
  without creating a new Cognito user.
- `<moduleN>`: the industry slug(s) this tenant licenses (space or comma
  separated). This is the ADR 016 read boundary — get it right the first time;
  see the failure mode below if you don't.
- `--email` triggers `tenant_config.cognito_upsert_user`: creates the Cognito user
  (sends an invite email via Cognito's own delivery) with `custom:tenant`/
  `custom:tier` set, or updates `custom:tier` if the email already exists **for
  the same tenant**. Omit `--email` to provision entitlement only and create the
  login separately later.
- Confirm the output line: `OK  <table>  <tenant-id>  tier=saas  plane=saas
  modules=[...]` followed by `OK  cognito  <email>  created  tenant=<tenant-id>
  tier=saas`.

## 4. First login at `/exec`

- Have the buyer complete the Cognito invite-email flow (set password) and log in
  at `https://<dashboard-domain>/exec`.
- Confirm the sector picker shows **only** the licensed module(s) — not the full
  17-sector list. If it shows everything, stop and see the read-boundary failure
  mode below before letting the buyer continue.
- Confirm `?admin=1` is **not** how the buyer is accessing the dashboard — that
  query param is the operator/curator full-feed bypass (still behind shared
  basic-auth), never a customer-facing link.

## 5. First weekly brief

Weekly digest push is gated OFF by default (`ONCA_WEEKLY_DIGEST=false`) and,
independently, blocked account-wide until SES production access clears (#111).
Once both are true for real customer delivery:

- Confirm `ONCA_ALERT_EMAIL_FROM`/`ONCA_ALERT_EMAIL_TO`-equivalent config for this
  tenant's recipient(s) is set (currently a shared, not per-tenant, delivery
  config — see `src/dashboard/weekly_digest.py`; per-tenant recipient lists are
  not yet built, flag this to the buyer if they expect distinct recipients per
  sector).
- Until then, send a manual proof to the buyer the same way #111 was verified:
  `weekly_digest.send_email(weekly_digest.weekly_scope(feed, sector="<their-sector>"),
  sender=..., to=<buyer-email>, dashboard_url=...)` from an operator shell — this
  is the exact mechanism that will run automatically once the digest is switched
  on for real, so a working manual send is a legitimate go/no-go gate.

## 6. What to check on day 2

- Pipeline ran cleanly overnight: check the `OncaPipelineFailedAlarm` /
  `OncaPipelineTimedOutAlarm` CloudWatch alarms (#109) — no alarm firing.
- `feed.json`'s `as_of` advanced by one day; the tenant's licensed sector(s) show
  a non-empty `by_industry` entry in `executive.cso`/`cro`/`cco`/`cpo`.
- Ask the buyer to describe what they saw on first login — a "nothing here" report
  almost always means step 3's module list is wrong (see below), not a data gap.

## Known failure modes

**Entry-tier guard rejects a higher-tier module.** `put_tenant_config` raises
`ValueError` if `tier="entry"` and any module is outside
`ENTRY_INDUSTRIES = (agri-funds, betting, consorcio, crypto,
real-estate-funds)` — the command fails loudly and provisions nothing (not a
partial write). This is correct behavior, not a bug: entry tenants are capped by
design (ADR 016). If the buyer needs a non-entry-tier vertical, use `saas` or
`sovereign`, not `--force-not-ready` (that flag is for the *not-launch-ready
industries* guard below, unrelated).

**Not-ready industries are rejected on every tier without `--force-not-ready`.**
`closed-pension`/`securitization`/`private-markets` are rejected even for
`saas`/`sovereign` tenants (#119) — this is the only guard those tiers get, and it
exists because those sectors are thin across every officer persona (step 0). Only
override with `--force-not-ready` for a named buyer with an explicit ingestion
plan you've separately agreed to — never as a way to make a demo look fuller.

**Wrong `modules` list silently shows an empty (not broken-looking) feed.** The
read boundary (ADR 016) is fail-closed: an unprovisioned tenant, or one with an
empty/wrong `modules` list, gets a clean "no access" gate or an empty sector
picker — **not an error message**, because to the code this is indistinguishable
from "correctly entitled to nothing." If a freshly onboarded buyer sees an empty
dashboard, the first thing to check is `provision_tenant.py get <tenant-id>` —
almost always the module slug was mistyped (e.g. `banks` instead of `banking`;
slugs are exact strings, not fuzzy-matched) or never applied because step 3's
command actually failed on the entry-tier guard above and you missed the
`REJECTED` line in the output.

**Cognito user already linked to a different tenant.** `cognito_upsert_user`
raises if the email exists with a *different* `custom:tenant` already set — this
is deliberate (the claim is immutable) and means the buyer's email was already
used for another tenant (a prior pilot, a demo account). Use a different email or
create a new Cognito user under a different username for the new tenant.
