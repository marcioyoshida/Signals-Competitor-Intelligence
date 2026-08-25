# ADR 013 — Queryable entity-classification attributes (ownership nature, compliance) + distress confidence tiers

- Status: **Accepted & implemented (v1)** — 2026-08-25. Owner-requested.
- Extends [ADR 012](2026-08-25-adr-distress-rj-store.md) (distress store) with the
  **trusted-seed + double-validation** model, and generalizes the pattern to other
  **per-entity attributes that were queryable-in-principle but not scoped**: a
  four-way **ownership/control nature** and **compliance certifications**.
- Code: `src/synth/entity_registry.py` (attributes + backfills), `src/synth/distress.py`
  (A+B+C), `src/dashboard/feed_builder.py` (`feed.json.entity_attrs`),
  `src/dashboard/agent_ask.py` (scope + grounding), tests.

## Context

The agent ([ADR 010](2026-08-25-adr-agent-chat-ui.md)) can only answer what the data
*scopes and grounds*. "Quais empresas estão em recuperação judicial?" failed until the
distress store existed **and** was wired into the agent — a reminder that a new queryable
facet needs (1) a data home, (2) a scope cue, (3) a grounding surface. The owner asked to
finish the distress store (trusted seed + double validation) **and to look for other alike
entity queries not yet scoped** — e.g. "is it ISO-certified?", or "is the entity public,
governmental, mixed (public+private capital), or private?".

Two kinds of entity signal fall out:
- **Curated/stable attributes** → belong on the `ENT#` registry record (single source of
  truth), like `aliases`/`industries`/`ticker`. → **ownership**, **certifications**.
- **Observed event-state** → a derived store with dates/evidence. → **distress** (ADR 012).

## Decision

### 1. Distress store — A + B + C (finish ADR 012)
- **A. Trusted structured seed = CVM Fato Relevante.** `update_from_digest` now mines the
  **`fatos`** stream (a listed issuer's own regulator-filed RJ) *and* news. `source_kind`
  maps CVM→`regulatory`, DataJud→`court`, else `news`.
- **B. Double-validation confidence tier** on every record: `sources[]` accumulates
  independent signals; `compute_confidence` grades **regulatory** (a CVM filing) >
  **curated** > **corroborated** (≥2 independent — a court record + news, or ≥2 distinct
  news publishers) > **reported** (single outlet, provisional). News now *upgrades* a CVM
  record instead of competing; a private-company RJ stays `reported` until a 2nd source
  confirms — the accuracy discipline for a defamation-sensitive claim.
- **C. Curated seed** `seed_distress()` at `curated` confidence. `SEED_DISTRESS` is
  **deliberately empty**: asserting a real company is in RJ/falência is accuracy-critical,
  so records must come from a filing (A), corroborated press (B), or a *vetted* analyst
  entry — never a fabricated guess. The mechanism exists; the list is filled from evidence.

### 2. Ownership / control nature (curated + derived registry attribute)
A four-way `ownership` field on `ENT#`: **public** (companhia aberta/listed), **governmental**
(wholly state-owned), **mixed** (sociedade de economia mista — public control + private
capital), **private**. `classify_ownership` = curated `OWNERSHIP` override (the non-derivable
state-owned / economia-mista cases) → else `public` if the entity is listed (has a ticker or
CVM fatos identity) → else `private`. `backfill_ownership` is an idempotent seed-style
migration (data, no deploy) — applied live to all 156 entities (119 private / 33 public /
3 mixed / 1 governmental: Caixa governmental, BB + BB/Caixa Seguridade mixed).

### 3. Compliance certifications (curated, evidenced)
A `certifications: []` field + `set_certifications`/`backfill_certifications`. The seed
(`CERTIFICATIONS`) is **conservative/empty by design** — a certification claim is
accuracy-critical, so it is populated only from a verifiable disclosure (analyst curation
or a future evidence-backed detector), never assumed. The attribute is **scoped and
queryable now**; empty answers say "nenhuma registrada" rather than inventing.

### 4. Make them queryable (the reusable wiring)
- **Surface:** `feed.json.entity_attrs` — a compact `{entity_id: {label, ownership,
  certifications, ticker, industries}}` map over the **whole registry** (not just
  narrative-bearing entities), so "quais são as estatais?" works even for quiet entities.
- **Scope:** ownership + compliance cues added to the agent's `_DOMAIN_CUES`.
- **Grounding:** `entity_fact_cards` projects `entity_attrs` into citable `fact:<entity>`
  cards (with PT ownership synonyms embedded so singular/plural queries match the
  exact-token search), folded into the grounding pool next to `distress_cards`.

## Guardrails
- **Accuracy first** — distress + certifications are evidence-gated, never assumed;
  ownership derivation is conservative (curated override for the only non-derivable cases).
- **Registry as source of truth** — ownership/certifications are API-editable `ENT#` data
  (no deploy), preserved across `put_entity` re-upserts (threaded through `assign_ticker`).
- **Defamation/LGPD** — same public-role discipline as distress/operatives.

## Consequences
- The registry becomes a **queryable classification surface**: ownership, certifications,
  ticker, industries, distress — all answerable by the agent + visible in the feed.
- The **pattern is now cheap to extend**: a new attribute = a curated `ENT#` field + a
  backfill + one line in `entity_attrs` + a scope cue. Candidates spotted for later:
  regulator/licence class (BCB/CVM/SUSEP), headquarters state/region, foreign vs domestic
  capital, cooperative vs S.A., ESG/rating, listing venue (B3 vs Nasdaq BDR).

## Status / next steps
Implemented v1; deployed. Next: (1) a dashboard classification facet/filter (ownership,
certified); (2) an evidence-backed certifications detector (news/fatos "certificada ISO");
(3) fill `SEED_DISTRESS`/`CERTIFICATIONS` from vetted sources; (4) the other attribute
candidates above as demand appears.
