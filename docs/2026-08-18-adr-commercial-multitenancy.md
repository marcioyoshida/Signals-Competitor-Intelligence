# ADR 002 — Commercial packaging, multi-tenancy, and IP protection

- Status: **Proposed** (2026-08-18)
- Extends [ADR 001](2026-08-17-adr-entities-registry.md) (entities registry).
  ADR 001 steps 6 (per-tenant config) and 7 (accounts/UI) are the commercial
  slices; this ADR records the pricing, entitlement, IP-protection, and tenancy
  decisions those slices must honour — and reorders them.

## Context

Three threads converged while planning monetization:

1. **Commercial model.** Subscribers belong to an **industry**, sold as
   module/add-on packages. Concentrated, high-gross industries (e.g. banking,
   investment banking) command a premium; fragmented, high-churn ones (e.g.
   fintech) are entry-priced. Pricing is *still open* because we don't yet know
   how much data each industry aggregates from the sources.
2. **Entitlement enforcement.** We ingest **broadly once** (one shared pipeline —
   required to catch unknown entrants; cheap). If modules were a client-side
   filter over one shared `feed.json`, unpaid data would already be in the
   browser. Client-side filtering is **obfuscation, not enforcement**.

   *Discovery vs. registry (a distinction that's easy to conflate):*
   **unknown-entrant discovery requires ingesting at least one enumerable source
   wholesale** (BCB authorized-entities list, CVM datasets) and diffing it — an
   inherently shared, non-per-tenant pass; you cannot diff a list you never
   downloaded, and per-**term** sources (news/DOU, one call per name) can only
   find names you already supplied. The **registry** is downstream of discovery:
   it is the shared *memory* that persists each find and makes it resolvable for
   **every** tenant, with no tenant ever having named it. So the registry
   decouples *availability* from any tenant's watchlist, but does **not** remove
   the need for the wholesale shared *sensor* that feeds it. Chain:
   *wholesale shared ingest → discovery → registry (persistence) → available to all.*
3. **IP protection.** The curated entities registry — and the raw/digest
   artifacts that embed it — are the moat. They must not ship to tenants, and
   their footprint (S3, DynamoDB, the repo seed) must be minimized.

## Decisions

### 1. Industry taxonomy (controlled, hierarchical — not freeform strings)
Example industry lists overlap and mix levels. Encode a **small canonical set**
(synonyms folded via the registry's existing alias-normalization), under a
parent sector. Provisional set (commercial owner confirms boundaries):

| Module | Folds in | Concentration → tier (provisional, not final pricing) |
|---|---|---|
| `banking` | retail/commercial banks | few players → premium |
| `investment-banking` | capital markets | few players → premium |
| `insurance` | | mid |
| `asset-management` | | mid |
| `wealth-management` | | mid |
| `private-markets` | venture capital, private equity | few, high-gross → premium |
| `fintech` | financial technology, financial software | fragmented → entry |
| `financial-data-analytics` | financial data, financial analytics | mid |
| `advisory` | financial-services consulting | fragmented → entry |

- `financial services` is the **parent umbrella**, not a peer module.
- Stored as `IND#<slug>` items in the existing single table (display name,
  aliases, parent) — editable without redeploy.
- **Never emit a price as final.** All tier/price values are provisional until
  set from measured data (Decision 5).

### 2. Entity ↔ industry mapping
- Generalize the entity `sector` field to `industries: [slug, …]` (an entity can
  span several — e.g. a neobank is `banking` + `fintech`).
- **Auto-tag only the safe case** (CNAE / `is_fintech`); **propose** ambiguous
  classifications through the **step-5 review queue** (reuse, don't rebuild).

### 3. Entitlement enforcement — server-side, at the data boundary
The tenant's module list is **server-authoritative**, derived from the
subscription/billing record; the client never supplies or edits it. Enforce so
the client **never receives non-entitled data**. Two acceptable patterns:

- **A — Authenticated feed API.** Site authenticates → token → `GET /feed`; a
  Lambda returns only entitled narratives (and entitled aggregates). No superset
  is ever published. Simplest; loses static-file CDN caching.
- **B — Per-module static shards + CloudFront signed cookies.** Feed builder
  emits `feed/<module>.json` shards; login issues signed cookies **path-scoped**
  to the tenant's modules; a request for an unpaid shard → 403 at the edge.
  Shards are shared across tenants of a module (cacheable, cheap). More parts
  (key pair, cookie issuer), best fit for the CDN cost model.

Start with **A** for correctness; migrate to **B** if CDN economics matter.

**Non-negotiables (either pattern):**
- Entitlements are server-derived; a tampered request just earns a 403.
- Authorize every read (`requested_module ∈ tenant.modules`).
- Never ship the superset.
- **Scope derived data too** — KPI counts, entity dropdown, search/autocomplete,
  timelines computed over the full set leak the *existence/volume* of unpaid
  modules. Compute them over the entitled subset.
- Cross-tagged entities: a narrative belongs to the **union** of its entities'
  industries; show it if that set intersects the tenant's modules.
- Audit who accessed what.

### 4. IP protection — the registry is never shipped
Today's state is already correct and must stay so: the registry lives in
DynamoDB; only the pipeline and internal (basic-auth'd) curation tools read it;
`feed.json` carries only a **derived projection** (`entity` slug, `label`,
`timeline`, `peak_score`, `total`). Rules:

- **Projection, not passthrough.** Split entity fields:
  - **Server-only (IP):** `aliases`, `alias_forms`, `cnpj_roots`, `controllers`,
    `confidence`, `canonical_id`, `needs_review`, `sources`, `ispb`.
  - **Shippable:** opaque `entity_id`, `label`, per-tenant-derived timeline/score.
- **Tenants never get DynamoDB creds / SDK / scan access** — reads go through an
  authorized endpoint only.
- **Three tiers of the entity asset:**

  | Tier | Contents | Who reads |
  |---|---|---|
  | Registry (DynamoDB) | full curation — the IP | pipeline + internal curation only |
  | Derived entity view | slug, label, timeline, score | tenant's **entitlement-scoped** feed |
  | Resolution service | name → canonical entity | pipeline-internal only |

**Anti-exfiltration — the registry is a saleable data asset (sell lookups, not
dumps).** Single-record-only is *necessary but not sufficient*: N single requests
= a scan. Protect the asset with the full stack:

- **Resolve, don't enumerate — within entitlement.** The only live entity endpoint
  is `GET /entity/{id}` for an id the tenant **already legitimately holds** (an
  entity in their entitled feed), authorized against their modules. Not "any valid
  id." This shrinks the reachable surface from the whole table to what they paid
  to see. No `list`/`scan`/`batch` endpoint exists.
- **Entitlement authz, not id-secrecy, is the control.** Current entity_ids are
  readable slugs (`nubank`, `zapbank_11222333`) — trivially enumerable, so id
  secrecy protects nothing. Authorize by "is this id in your set?", not by hoping
  ids stay hidden. (Opaque per-tenant handles are a hardening option, not the gate.)
- **Rate limits + volumetric anomaly detection.** Cap lookups/tenant/time and flag
  *breadth* — a tenant resolving thousands of distinct entities is copying, not
  using. Breadth-of-distinct-ids is the copying signature.
- **Legal + forensic.** License/ToS forbids redistribution/scraping; seed
  **canary records** (per-tenant fingerprints) so a leaked copy traces to its
  source.
- **Honest ceiling:** copying is made *uneconomic and detectable*, not impossible
  — rendered data can always be harvested slowly. This is layered deterrence
  (technical + legal + forensic), the same bar every data vendor lives with.
- **Scope of the rule:** the *derived scoped feed* is delivered in bulk (that is
  the product, already IP-stripped); the *registry* is never bulk-accessible. The
  no-scan rule applies to the **asset**, not the product.
- **Marketplace fit:** single-record maps 1:1 to AWS Marketplace **metered
  per-lookup** billing, or to a **module subscription** with a fair-use rate cap.
  The marketplace does billing/entitlement; **data protection stays with the
  seller** — it is not provided for you.

### 5. Pricing is measured, not guessed
Pricing ≈ f(industry **concentration**, **data richness**). Both are
**measurable from the pipeline we already run**: concentration = count of
distinct tracked entities per industry (few = concentrated = premium); richness =
signal/narrative volume per industry over time. Build a **per-industry
measurement instrument** (internal rollup) so tiers are set from evidence. No
fabricated numbers; every provisional figure carries an "estimated" label.

### 6. S3 / at-rest IP posture (audit 2026-08-18)
Live audit: all buckets have full Block Public Access; site bucket is locked to
CloudFront OAC + TLS-only; `digests`/`raw` are SSE-S3 with **no bucket policy**
(IAM-only); **account-level** Block Public Access is **not set**. Actions:

- **Applied 2026-08-18 (Phase A — all additive; no bucket dropped, no data
  deleted).** Steps live in `infra/harden_buckets.sh` (idempotent) since these
  buckets predate the stack (imported by name):
  - Bucket policy on `digests`/`raw`: **Deny non-TLS** + **Deny cross-account**
    (`aws:PrincipalAccount != this account`). Same-account form, so it cannot lock
    out the in-account Lambda/Bedrock roles — verified: feed-builder still reads
    digests (feed_count 72) and in-account reads intact on both.
  - **S3 server access logging** on both → locked-down `onca-s3-access-logs-<acct>`
    (BPA + SSE + log-delivery-only policy).
  - **Account-level Block Public Access** enabled (account-wide guardrail).
  - **Lifecycle:** expire `onca-raw` objects after **180 days** (raw corpus only,
    not digests/narratives). Forward-looking — oldest object was ~37 days old at
    apply time, so nothing deleted then.
- **Still pending (need a decision — not auto-applied):** per-role principal
  allowlist (tighter than same-account; needs enumeration + testing); SSE-KMS CMK
  on `digests`/`raw` (read then also needs `kms:Decrypt` — a second gate).
- **Strategic:** make the **registry the single source of truth** for the curated
  list; leave only a minimal bootstrap seed in git (removes the repo as a leak
  surface). `config/watchlist.yaml` + `ENTITY_ALIASES` shrink to a seed.

### 7. Identity before modules (reorders ADR 001)
You cannot scope what you cannot identify. **Per-tenant identity (ADR 001 step 7,
Cognito/accounts) is a prerequisite for paid modules (step 6).** Shared basic-auth
cannot attribute entitlements. The numbered order flips: **7 gates 6**.

### 8. Tenancy deployment boundary — vendor account vs tenant account
The default is **pure SaaS**: everything runs in the **vendor / registry-owner
account**; the tenant gets a login only. The boundary rule: **anything that could
reconstruct the registry stays vendor-side; tenants receive only scoped,
projected, entitlement-gated outputs.**

| Concern | Vendor / registry-owner account (the moat) | Tenant account (consumption edge) |
|---|---|---|
| Entities registry (DynamoDB) | ✅ owns — never leaves | ❌ never |
| Ingestion pipeline, sources, Step Functions | ✅ | ❌ |
| Raw + digest S3 (entity-derived intelligence) | ✅ | ❌ |
| Synthesis / fusion / LLM narratives | ✅ | ❌ |
| Review queue + curation tooling | ✅ | ❌ |
| Industry taxonomy + measurement | ✅ | ❌ |
| Entitlement / billing source of truth | ✅ | ❌ |
| Authenticated read API / shard signer | ✅ | ❌ |
| Identity (Cognito user pool) | ✅ (hosted) | — (tenant users are pool members) |
| Dashboard hosting | ✅ default (SaaS) | ⚪ optional thin stack (Marketplace/embedded) pointed at the vendor API |
| Entitlement-scoped data delivery | ✅ produces it | ⚪ optional: vendor writes the tenant's **scoped** feed to a **tenant-owned S3 bucket** (cross-account) for data residency |

- **Pure SaaS (default):** tenant owns nothing; best IP protection.
- **Marketplace / embedded (optional):** a thin CFN stack in the tenant account —
  dashboard hosting and/or SSO integration — that **only calls the vendor's
  authorized API**. The registry and pipeline never deploy there.
- **Bring-your-own-bucket (optional, enterprise):** for data-residency needs, the
  vendor writes the tenant's **entitlement-scoped, projected** feed into a
  tenant-owned bucket. Still no registry, no superset, no raw/digest.

## Phase plan (supersedes ADR 001 steps 6–7 ordering)

- **Phase A — Foundations & IP hardening (now).** S3 TLS-only policies + access
  logging + lifecycle (zero-risk); account BPA (confirm); registry-as-source-of-
  truth / thin repo seed. Document projection tiering + resolve-not-enumerate.
- **Phase B — Taxonomy & measurement.** `IND#` taxonomy, `entity.industries[]`,
  auto-tag safe case + propose the rest (step-5 reuse), per-industry
  volume/concentration instrument. Unblocks pricing.
- **Phase C — Identity (ADR 001 step 7, pulled forward).** Cognito user pool;
  per-tenant identity; upgrade the review-queue write-path off shared basic-auth.
- **Phase D — Entitlement read boundary.** `onca-tenant-config` with `modules[]`;
  pattern A authenticated feed API (or B signed shards); scope feed + aggregates.
  Registry-as-asset controls: resolve-only `GET /entity/{id}` scoped to the
  tenant's entitled set (no list/scan/batch), per-tenant rate limits + breadth
  anomaly detection, canary records for leak forensics.
- **Phase E — Packaging, pricing, billing.** Module catalog; data-driven tiers
  (from Phase B); Marketplace integration; choose tenancy deployment options.
- **Phase F — Manage-entities UI.** Curation UI over the review queue; admin.

Phases A–B are safe to do before any tenant exists. C gates D and E. Nothing that
gates entitlements ships before C.

## Consequences

**Positive**
- Broad shared ingest keeps premium modules near-pure-margin.
- Registry stays a private moat; tenants get only scoped projections.
- Pricing becomes evidence-based; no guessing, no fabricated figures.
- Marketplace/residency options exist without ever exposing the IP.

**Costs / risks (honest)**
- Entitlement + projection discipline touches the read path everywhere; the
  subtle leak is *derived* aggregates, not the cards — easy to miss.
- Identity-before-modules means real revenue waits on the accounts layer.
- Per-tenant scoping erodes the single-static-file simplicity (pattern B recovers
  most caching; pattern A trades it for a Lambda read).
- Taxonomy boundaries are a commercial judgement, not a technical one.

## Alternatives considered
- **Client-side module filter over one shared feed** — rejected; obfuscation, not
  enforcement (superset reaches the browser).
- **Tenant-facing query/scan API on the registry** — rejected as a general read;
  it is scraping-by-API. Only resolve-by-known-id, projected, rate-limited.
- **Deploying the pipeline/registry into tenant accounts** — rejected; ships the
  moat. Tenant-side is at most a thin consumption stack against the vendor API.
