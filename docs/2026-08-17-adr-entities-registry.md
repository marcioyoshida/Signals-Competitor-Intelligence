# ADR 001 — Entities registry, per-tenant watchlists, and auto-mapping

- Status: **Accepted** — rollout steps 1–5 shipped (2026-08-18); 6–7 pending.
  Commercial/multi-tenant/IP-protection concerns for steps 6–7 are recorded in
  [ADR 002](2026-08-18-adr-commercial-multitenancy.md).
- Supersedes the hardcoded `config/watchlist.yaml` entity lists and the static
  `ENTITY_ALIASES` dict in `src/synth/entities.py`.

## Context

Three problems surfaced while discussing scale:

1. **Deploy-baked config.** `config/watchlist.yaml` is read at `cdk deploy` time
   and frozen into the Lambda env. Changing the tracked competitors needs a
   CloudFormation update — untenable for a product where users edit their list.
2. **Single global list, no per-tenant.** Each corporate user has a different
   competitor niche, but there is one global watchlist. Yet most of the sector
   (BCB/CVM/SUSEP/CADE acts, the big banks + fintechs) is relevant to *everyone*,
   and we must ingest broadly to catch **unknown** entrants a watchlist can't name.
3. **Entities aren't persisted.** The `new_entrants` (registry-diff) lens + Receita
   QSA enrichment discover a quietly-registered fintech *once*, but because it
   never enters `ENTITY_ALIASES`, the **next** signal about it (a CVM offering, a
   news headline, a DOU act) fails to resolve/cluster. Aliases are hand-maintained.

Two "entity" concepts exist today and should converge: the watchlist *names to
track* (config) and `ENTITY_ALIASES` the *canonical registry* used for
resolution / clustering / parent-linking.

## Decision

**Broad shared ingestion + per-tenant read-layer personalization + a
self-expanding entities registry in DynamoDB, with CNPJ-keyed auto-mapping.**

### 1. Layering (resolves "broad vs. niche")
- **Ingest broadly** — one shared pipeline builds one broad feed for all tenants
  (cheap; also required to detect unknown entrants). Structured sources (CVM/IF.data
  CSVs) filter a downloaded file → broaden freely. Per-term sources (news, DOU) do
  one HTTP call per name → cap at ~15–20 terms.
- **Personalize at read time** — scoring boost, feed filter, and alerts use a thin
  per-tenant profile. Never a per-tenant pipeline.

### 2. Entities registry (unifies watchlist + ENTITY_ALIASES)
Single-table DynamoDB design with typed lookup keys for O(1) exact resolution
(no GSI needed):

```
onca-entities
  PK = "ENT#<entity_id>"      -> entity record
       display_name, canonical_id (group leader), aliases[], cnpj_roots[],
       ispb, ticker, sector, license_class, controllers[],
       confidence ("curated"|"cnpj"|"fuzzy"), needs_review, active,
       first_seen, last_seen, sources[]
  PK = "CNPJ#<root8>"         -> { entity_id }     # exact join key
  PK = "ALIAS#<normalized>"   -> { entity_id }     # name-index (accent-folded)
```
Upsert writes the `ENT#` item plus one `CNPJ#` and N `ALIAS#` items.

### 3. Per-tenant config
```
onca-tenant-config  PK = "<tenant_id>"
  watchlist [entity_id, ...]        # boosts scoring / filters view / drives alerts
  score_weights, alert_prefs, thresholds
```
Thresholds/flags that are true *config* (lookback days, move %, use_competitors)
live here or stay in env — do **not** force them into the entities table.

### 4. Auto-mapping mechanism (the self-expanding part)
Resolution order for any signal:
1. **Has CNPJ** → `GetItem CNPJ#<root>`. Hit → entity. **Miss → auto-create**
   an `ENT#` record seeded from the signal + Receita enrichment (razão social,
   nome_fantasia, license class, QSA controllers), with `confidence="cnpj"`; write
   its `CNPJ#` and `ALIAS#` items.
2. **Name only** → normalize (accent-fold) → `GetItem ALIAS#<normalized>`.
   Hit → entity. Miss → unresolved (optionally queued as a fuzzy candidate).
3. **Alias accumulation** — every source that names an entity (CVM "BANCO X S.A.",
   news "X", ticker "XPTO3") adds to that entity's aliases → recall grows over time.

**Confidence / review model** (entity resolution is hard — automate only the safe
parts):
- ✅ **Auto:** CNPJ-exact mapping; data-derived aliases (razão social, nome fantasia,
  ticker).
- ⚠️ **Propose, don't auto-commit:** grouping multiple CNPJs into one brand/parent
  (hint = shared QSA controller) via `canonical_id`; fuzzy name-only matches;
  colloquial nicknames (LLM may *suggest*, human confirms). These set
  `needs_review=true` and surface in a review queue.
- The hand-curated `ENTITY_ALIASES` becomes **seed data + trusted overrides**
  (`confidence="curated"` wins over auto).

## Consequences

**Positive**
- Aliases self-expand from the pipeline we already run (registry-diff + Receita);
  no redeploy to add/track an entity.
- Foundation for multi-tenant SaaS (shared ingest + per-tenant profile).
- Recognition/recall of quiet entrants improves the more they appear.

**Costs / risks (honest)**
- Entity resolution / dedup is a classic hard problem. **Bad-merge risk** is real
  (StoneX ≠ StoneCo) — mitigated by keying on CNPJ and gating fuzzy/group merges
  behind review.
- Signals without a CNPJ (news, DOU) still rely on name aliases — never perfect,
  but improving.
- Runtime dependency on the table (mitigate: load the alias/CNPJ maps once per
  Lambda run and cache in-memory; the registry is small — thousands of items).
- Migration effort; `entities.py` and the ingesters must read (and cache) from the
  registry, with the in-memory `ENTITY_ALIASES` kept as a graceful fallback/seed.

## Alternatives considered
- **S3 JSON object / AWS AppConfig** for the config — simpler way to decouple from
  deploy, but weak for per-tenant querying and a CRUD UI. Acceptable *interim* if
  we only want to stop redeploying; not the destination.
- **Name as primary key** — rejected; fuzzy and collision-prone. CNPJ is exact.
- **Fully-automatic resolution (no review)** — rejected; produces bad merges.

## Rollout (incremental)
1. Create `onca-entities`; seed from `ENTITY_ALIASES` (`confidence="curated"`) +
   watchlist names (CDK).
2. Registry-backed `resolve_entities` — load + cache the ALIAS#/CNPJ# maps at run
   start; fall back to in-memory `ENTITY_ALIASES` if the table is unavailable.
3. Auto-create/upsert on every observed CNPJ — start in the entrant + Receita path
   (payload already exists), then extend to all CVM signals. **Shipped 2026-08-18**
   (`auto_create_from_entrant`, wired in `lambda_port.py`, live-tested).
4. Alias accumulation from all sources. **Shipped 2026-08-18** (first slice):
   `accumulate_aliases` folds a structured CVM signal's razão social
   (offering `issuer` / fato relevante `company`) into the entity its CNPJ
   already resolves to — data-derived + CNPJ-gated, the auto-safe case. A
   normalized name owned by a *different* entity is never hijacked (left for the
   step-5 review queue). Wired as a best-effort pass in `lambda_port.py`
   (`ONCA_ENTITIES_ACCUMULATE`, default on). *Follow-up:* extend producers to SEC
   ticker/company and auto-create-on-CNPJ-miss for CVM issuers.
5. Review queue (`needs_review` items surfaced in the dashboard) + curation.
   **Backend shipped 2026-08-18** (first slice): a `REVIEW#` queue in the same
   table with `propose_review` (idempotent by kind+key), `list_reviews`,
   `resolve_review` (approve applies the change, reject records the decision so
   it isn't re-proposed). First producer: `propose_group_merges` — entities
   sharing a QSA controller get a *proposed* `canonical_id` link (never
   auto-merged; StoneX ≠ StoneCo), curated member preferred as leader. Wired in
   `lambda_port` after auto-create (`ONCA_ENTITIES_REVIEW`, default on; scans
   only when a new entrant appeared). **Read-only surface shipped 2026-08-18**:
   the feed builder reads pending `REVIEW#` items (`entities_table` read grant),
   resolves slugs to display names, and rides them on `feed.json` as `reviews[]`;
   the dashboard shows a "Revisão de entidades" panel (visible only when
   proposals exist), stating nothing is auto-merged. **Write-path shipped
   2026-08-18** — step 5 complete: a `review_action` Lambda (Function URL) behind
   a CloudFront `/api/*` behavior gated by the *same* basic-auth edge function as
   the dashboard, so the browser's existing credentials authorize approve/reject
   (Aprovar/Rejeitar buttons POST to `/api/review`); the action applies via
   `resolve_review` and triggers a feed rebuild. The Function URL is AuthType
   NONE with a shared origin secret (CloudFront-injected header) so direct calls
   are rejected. Verified live: approve via CloudFront+basic-auth applied the
   `canonical_id` merge (200); direct Function-URL call → 403; no-auth → 401.
   Chose reused basic-auth over Cognito to keep step 5 self-contained; full
   accounts/identity remains step 7.
6. `onca-tenant-config` + read-layer personalization (scoring/filter/alerts).
7. Manage-entities UI (with the Cognito accounts layer).

Steps 1–4 are the high-value core (kills the "redeploy to change competitors"
pain and makes the radar self-expanding); 5–7 come with the multi-tenant/account
work.
