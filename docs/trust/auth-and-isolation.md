# Auth & isolation

How a buyer's data (and other tenants' data) is protected. The design principle is
**fail closed**: absence of a valid identity or entitlement record means no data,
never a default-open fallback.

## Authentication

- **Amazon Cognito**, PKCE Hosted-UI login — no password is ever handled or stored by
  Onça application code. Tenants are provisioned, not self-service
  (`self_sign_up_enabled=False`).
- Every user carries two custom claims: `custom:tenant` (**immutable** once set — a
  user cannot be silently relinked to a different tenant) and `custom:tier` (mutable,
  e.g. an entry→SaaS upgrade), plus a 12-character minimum password policy.
- The four tenant-data API routes (`/api/ask`, `/api/gaps`, `/api/feed`,
  `/api/registry`) sit behind an API Gateway `HttpJwtAuthorizer` bound to the Cognito
  pool — API Gateway verifies the token's signature/issuer/audience and only forwards
  a request that already passed. Application code never decodes a raw bearer token
  itself; it reads claims exclusively from the field API Gateway populates after
  verification, so no claims present resolves to "unauthenticated," never a default
  identity.
- A second, lighter-weight entitlement path exists: plain Cognito **group**
  membership (one group per industry, e.g. `banking`). Group names are validated
  against the canonical industry taxonomy before being trusted, so a role group
  (`operator`/`admin`) or a typo can never resolve to a bogus module.

## Per-tenant read boundary

- `OncaTenantConfig` (DynamoDB) is the single source of truth for what a tenant is
  entitled to see: `{tenant_id: {tier, modules[]}}`.
- `GET /api/feed` requires a verified identity **and** a non-empty module list, in
  that order — checking the tenant row first, falling back to Cognito groups only if
  the tenant row yields nothing. An unprovisioned tenant, or one with an empty
  `modules` list, gets a `403`, never a default view of everything.
- The projection is enforced **server-side**: the Lambda reads the complete feed,
  scopes it down to the caller's licensed modules, and returns only the result — the
  full feed never leaves the Lambda. Two operator-only fields (integrity findings,
  regulatory-coverage map) are hard-zeroed for every tenant regardless of module
  list, not merely filtered. The client — the SaaS context screens and `/exec`'s
  normal (non-admin) path — calls only the scoped API and never fetches a raw,
  unscoped feed object.
- This was independently verified against the live, deployed Lambda across eight
  real tenant rows (single-vertical, multi-vertical, entry-tier, sovereign-plane):
  every officer dashboard (CSO/CRO/CCO/CPO) showed exactly that tenant's licensed
  sectors, zero leakage in any cell.

## Delivery planes and isolation strength

Three delivery planes, in increasing isolation:

1. **Entry** — one shared static feed, capped at feed-build time to five entry-tier
   industries; higher-tier data is never written into the object, so there is
   nothing to leak by scoping. Every Entry subscriber sees the same shared slice by
   design, not by tenant — this is the Portal model, not a gap.
2. **SaaS** — shared infrastructure, isolation is entirely the application-layer
   mechanism above (server-side per-tenant projection behind the JWT authorizer).
3. **Sovereign / in-account (Marketplace)** — the entire stack deployed directly
   into the customer's own AWS account. No shared infrastructure at all; isolation
   is an AWS account boundary, not application logic — the strongest of the three.

A buyer choosing between SaaS and Sovereign should understand this is a genuine
difference in isolation *mechanism*, not just price.

## The coarse edge gate

Every path on the shared CloudFront distribution — dashboard pages and static
assets — additionally sits behind a single shared password checked at the edge.
This is a coarse perimeter control (anti-scrape, keep-random-people-off-the-internet),
**not** a tenant boundary — it is one password shared by everyone who uses the
product, and it never distinguishes tenants. `/pricing.html` is the one path
deliberately excluded from it.

Because this shared password is not tenant-specific, the raw `feed.json` object (the
complete, unscoped, every-tenant corpus) additionally requires a **second, narrower
operator-only secret** before it will be served at all — the shared tenant password
alone is not sufficient to read it. Only curation/admin tooling (`?admin=1` on
`/app`/`/exec`, `/v2/admin`) carries this second secret; no tenant-facing flow does,
and no tenant is ever given it. `feed.entry.json` is not gated this way — it is the
deliberately shared, entry-tier-only slice described above.

**The takeaway for a buyer:** the real isolation guarantee is carried by the
JWT-authorized API routes and the screens that call only them. The shared basic-auth
password is a coarse perimeter control; it is not, and is not relied on as, a
substitute for per-tenant isolation.

## Known history

- **2026-09-12 (issue #120):** `/exec` briefly fetched the full unscoped feed
  regardless of login, and separately, the per-tenant projection filtered every feed
  section except the executive-officer block — so a tenant licensed to one industry
  could see the full officer dashboard for all sectors. Found and fixed same day,
  verified against the live Lambda.
- **2026-09-13 (issue #122):** the raw `feed.json` object was reachable with just the
  shared tenant basic-auth password — the same password every tenant needs to reach
  the login screen — bypassing the per-tenant API entirely. Found while writing this
  page, fixed same day by requiring the second operator secret described above,
  verified live before this page was published.

We disclose both rather than imply a spotless history: each is the reason the
verification and the precise wording above exist at all.

## Operational backing

- The entities registry, tenant entitlement table, and curation audit log all have
  **point-in-time recovery** enabled and deletion protection turned on — the
  commercial asset has a tested restore path, not just an application-level
  rollback of individual fields.
- Pipeline failures and feed staleness are alarmed (CloudWatch) rather than failing
  silently — a buyer sees an honestly stale product, never a quietly broken one
  presented as current.

## Verify this yourself

- `grep -n "self_sign_up_enabled\|custom_attributes\|password_policy" infra/app.py`
- `grep -n "HttpJwtAuthorizer\|jwt_audience" infra/app.py`
- `sed -n '1,100p' src/dashboard/auth.py` — confirms claims are read, never a raw token decoded.
- `sed -n '1,70p' src/dashboard/feed_api.py` and `grep -n "def scope_feed_to_modules" -A 70 src/dashboard/feed_builder.py`
- `gh issue view 120 --repo marcioyoshida/Signals-Competitor-Intelligence --json body,closedAt`
- `gh issue view 122 --repo marcioyoshida/Signals-Competitor-Intelligence --json body,closedAt`
- `docs/2026-09-13-gated-sector-access-verification.md` — the live, per-tenant, all-officer scoping check.
