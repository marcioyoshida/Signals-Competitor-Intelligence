# ADR 017 — Conglomerate multi-tier disambiguation (sub-entities + card-level industry + tier-1 opt-in)

Status: ACCEPTED 2026-08-30. Relates to ADR 016 (distribution tiers), ADR 013
(entity classification attrs), ADR 011 (entity discovery), ADR 005/entities registry.

## Problem

Narratives are consolidated **by entity** (`candidates._cluster_by_entity` fuses all
of an entity's signals across all lenses into one `entity_fusion` card), and a card's
industry is inherited from the **entity's** industry set (`feed_builder` reads
`entity_attrs[e].industries`). That is fine for a single-line player, but large
conglomerates — **Itaú, Bradesco, Banco do Brasil, Caixa** (and BTG, XP) — operate
across every tier: tier-1 banking / investment-banking **and** lower-tier lines like
consórcio and FIAGRO/FII funds. Two failure modes follow:

1. **Leak (over-inclusion).** If the parent is tagged with a lower industry, ALL its
   cards — including tier-1 M&A / results — fuse into that entity and surface in the
   lower-industry view (e.g. a BTG "Tesouro Selic" fund card and its Macro Day appearing
   in the Entry Portal's FIAGRO slice). This is what triggered this ADR.
2. **Under-representation.** If we simply strip the lower industry off the parent (the
   BTG/BB hotfix), the conglomerate's *genuine* lower-industry activity disappears from
   the lower view — because those signals were attributed to the tier-1 parent.

Root insight: **a signal's industry is a property of WHICH ENTITY it is about, not of its
lens.** Lenses are data *sources* (`news`, `regulatory`, `pix`, `funds`…) and are almost
all cross-industry, so industry cannot be derived from the lens. Therefore the unit of
attribution — the entity — is where the disambiguation must happen.

## Decision

Adopt a **three-part** model (chosen over "split fusion by lens", which fails because lens
≠ industry):

### 1. Sub-entity modeling (primary mechanism)
A conglomerate's line of business is its **own entity** with a `parent` link to the
tier-1 entity and a **single lower industry**. Structured, CNPJ-identified filings
(FIAGRO/FII funds, consórcio registrations) attribute to the **sub-entity** by its own
identity; the tier-1 **parent stays tagged tier-1 only** and never carries a lower
industry. Precedent: BTG's FIAGRO funds already exist as their own entities
(`btg-ceres`, `btca11`, …) via discovery — generalize that. A sub-entity fuses only its
own line's signals, so its cards are correctly single-industry and scope cleanly.

- Registry: entities gain an optional **`parent`** field (`ENT#child.parent = "itau"`).
- The parent is discoverable from its children (`children_of(parent)`), enabling the
  tier-1 opt-in below and a "corporate group" rollup.

### 2. Card-level industry provenance (refinement / robustness)
Every feed card carries an explicit **`industries`** array (denormalized: the union of
its entities' registry industries). Scoping (Entry feed fork, per-tenant read boundary,
the dashboard toggles) filters by the **card's** industries, not by re-deriving from the
entity each time. With sub-entities in place a card's `industries` is its sub-entity's
single industry, so scoping is exact. This is the load-bearing primitive both delivery
tiers already lean on — making it explicit on the card future-proofs it.

### 3. Tier-1 opt-in toggle (never permanently hide)
A higher-tier tenant entitled to a conglomerate is entitled to its whole corporate group.
The lower-industry sub-entity cards are **hidden by default** in the tier-1 view (keep the
tier-1 signal clean) but a tenant **may toggle them on** ("show group's consórcio / funds
lines"). Mechanism: the parent↔child link lets the dashboard fold a parent's children's
cards in on demand; entitlement already permits it (the tenant licenses the parent's
tier). This is a *view* preference over already-entitled data, not a second paywall — the
inverse of the Entry Portal, where lower tiers can never see tier-1 (enforced at feed
build, ADR 016).

## Phasing

- **Phase 1 (this ADR's first cut — DONE 2026-08-30):** the durable foundation.
  - Card-level `industries` denormalized onto every feed card; Entry fork + agent scope by
    it (equivalent today, exact once sub-entities land).
  - Registry `parent` field + `set_parent`/`children_of` primitives.
  - Durable cleanup of the runtime FIAGRO-enrich pollution on `btg`/`bb` (the code-level
    brand-match fix `37fb048` already stops recurrence; the fixture already has the
    conglomerates as tier-1 only).
- **Phase 2:** create the first conglomerate sub-entities (Itaú/Bradesco/BB consórcio +
  fund lines) with `parent` links; route their structured filings to them; verify the
  lower views show the sub-entity, not the parent.
- **Phase 3:** the tier-1 opt-in toggle in the SaaS dashboard + a `children_of` rollup;
  entitlement passes through unchanged (group is under the parent's tier).

## Consequences
- Tier-1 views stay clean; lower views gain genuine conglomerate coverage without tier-1
  bleed; nothing entitled is permanently hidden.
- More entities (one per conglomerate line) — acceptable; discovery already does this for
  funds. Sub-entity attribution is only as good as the structured identity (clean for
  CNPJ filings; news about "Itaú's consórcio arm" still resolves to the parent — a known
  residual, same class as the manager==brand note in the FIAGRO memory).
- `parent`/`industries` are additive fields; existing consumers are unaffected.
