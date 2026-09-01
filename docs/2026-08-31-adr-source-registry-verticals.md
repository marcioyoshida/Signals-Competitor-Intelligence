# ADR 019 — Declarative Source Registry & First-Class Verticals

Status: ACCEPTED 2026-08-31; **IMPLEMENTED (core) — with named follow-ons.** The
registry-driven loop is now REAL: `lambda_port` runs `for spec in registry.active(vertical)`
over a `FETCHERS` registry that applies vertical-gating + budget + delta + section-building
uniformly, for the sector-agnostic + core document sources (regulatory, competitor, ofertas,
dou, cade, sanctions, contracts). Adding such a source = a `SourceSpec` + one `FETCHERS`
entry, no bespoke block and no payload edit. Still open (incremental, mostly matters for the
not-yet-live sectorial deployment): (a) the side-effecting lens sources (sec content-enrich,
fatos governance-sort + alias accrual, new_entrants receita/autocreate) and the numeric
`moves` + `market` sources stay bespoke — their in-block side effects/special shapes make a
clean move risky; (b) per-source knobs are co-located in the FETCHERS closures but not yet a
declarative per-spec config (still read from `watchlist.yaml`/env); (c) the non-lens stores
(datajud/reclamações/reclame-aqui/consumidor/macro) aren't `SourceSpec`s; (d) the bespoke FS
sources are output-gated, not fetch-gated, under a sectorial vertical; (e) the Anteater fork
is not retired — its sector entities/taxonomy/sources are a data migration. See "What's
missing".
Relates to ADR 011 (discovery/enrichment), ADR 003/006
(axes & strategy frameworks over lenses), the narrative-signal taxonomy ADR (#34 lens/topic),
ADR 015/016/017 (distribution tiers, Entry vertical scoping, conglomerate disambiguation),
and the [[onca-tier1-cross-industry-ingestion]] arc (#60–#63) that motivated it.

## Context

Ingestion has grown to **31 source modules** (`src/ingest/*.py`). Every one is wired by hand
in **three** places, and adding a source repeats all three (the "new-ingester recipe"):

1. **`lambda_port.py`** — an `import`, a fetch block under `_source_budget(...)`, a delta call
   (`_new_since_last_run` / `_moves_since_last_run` / a durable-store update), and a hand-built
   `digest["<section>"] = {count,new_count,items,context}` entry. The handler is now a
   ~600-line god-function of near-identical blocks.
2. **`candidates.py`** — the source's lens must be added to **four** hand-maintained frozensets/
   dicts: `LENS_WEIGHT`, `HIGH_VALUE_SOLO_LENSES`, `STRUCTURED_SUBJECT_LENSES`, `BACKDROP_LENSES`,
   **plus** the `_collect_signals` `sections` list (section→lens). Getting one membership wrong
   silently mis-scores or mis-clusters (exactly the #50 class of bug).
3. **`config/watchlist.yaml`** — per-source knobs (lookbacks, thresholds, watchlists) as a flat,
   FS-shaped keyspace (`pix_move_threshold_pct`, `dou_lookback_days`, …).

Shipping #60–#63 repeated this 4× and surfaced two structural problems:

- **O(n) boilerplate, error-prone.** A source's identity is scattered across ≥3 files; there is
  no single descriptor. The ad-hoc gating flags (`ONCA_PNCP_CONTRATOS`, `ONCA_CADE`,
  `ONCA_CEIS_CNEP`, `ONCA_CONSUMIDOR_GOV`) are the un-generalized form of "is this source active
  here?".
- **No vertical boundary.** Onça (financial-services) and the **Anteater** sectorial spin-off
  (pharma/health, logistics, retail, energy, telecom, agro) share this code as **divergent
  forks**. Yet #60–#63 are sector-agnostic sources built *"for the sectorial deployment"* living
  in the Onça repo — so the FS pipeline carries them and PNCP is `default-off` *precisely because
  it yields nothing for FS*. "Which sources / lenses / industries apply to which market" is
  implicit, hard-coded, and enforced by scattered env flags instead of declared.

These are one decision from two angles: a source needs a **single declarative descriptor**, and
that descriptor must carry **vertical applicability** so one codebase serves both products.

## Decision

Introduce a **Source Registry**: every ingestion source is described by one immutable
`SourceSpec`, and the pipeline + scoring are **driven by iterating the registry** instead of
hand-coded blocks. Verticals become a first-class dimension of the spec and of deployment.

### `SourceSpec` (single source of truth per source)

```python
@dataclass(frozen=True)
class SourceSpec:
    id: str                     # "cade", "ceis_cnep", "pncp_contratos", …
    fetch: Callable             # () -> list[doc]  (thin adapter over the module)
    lens: str | None            # candidates lens; None for macro/untied
    weight: float               # -> LENS_WEIGHT
    solo: bool = False          # -> HIGH_VALUE_SOLO_LENSES
    structured_subject: bool = False   # -> STRUCTURED_SUBJECT_LENSES
    backdrop: bool = False      # -> BACKDROP_LENSES
    resolution: str = "text"    # "cnpj" | "name" | "prebound(_entities)" | "macro"
    delta: str = "new"          # "new" | "moves" | "store" | "none"
    integration: str = "lens"   # "lens" (digest section + candidates) | "store" (index.json panel)
    cadence_days: int = 1       # rolling lookback / period
    verticals: frozenset[str] = frozenset({"all"})   # {"financial-services"} | {"retail","health",…} | {"all"}
    default_on: bool = True
    env_flag: str | None = None # optional override (back-compat for the existing ONCA_* flags)
```

### The registry drives everything

- **Pipeline.** `lambda_port` becomes: `for spec in REGISTRY.active(vertical, env):` → run
  `spec.fetch()` under `_source_budget`, apply `spec.delta`, emit the digest section (or update
  the durable store). The ~600-line god-function collapses to one loop + the specs. Wall-clock
  budgets, delta, and the seed-suppress rule move into the loop, uniform for all sources.
- **Scoring.** `candidates.py` **derives** `LENS_WEIGHT` / `HIGH_VALUE_SOLO_LENSES` /
  `STRUCTURED_SUBJECT_LENSES` / `BACKDROP_LENSES` and the `_collect_signals` section list **from
  the registry** — the four frozensets stop being hand-maintained. Getting lens policy right is a
  property of one spec, not four edits.
- **Config.** Per-source knobs move from the flat `watchlist.yaml` keyspace onto the spec /
  a per-source config block, keyed by `spec.id`.

### Verticals as a first-class abstraction

A **vertical** = { active source set, entity seed/universe, in-scope industry taxonomy, lens
applicability, delivery config }. The deployment selects one active vertical
(`ONCA_VERTICAL`, default `financial-services`); the pipeline runs `REGISTRY.active(vertical)` and
the registry/feed scope to that vertical's taxonomy. Sector-agnostic sources declare
`verticals={"all"}` (CEIS/CNEP sanctions, PNCP contracts, CADE antitrust); FS-only sources declare
`{"financial-services"}` (Pix, juros, IF.data, CVM funds, SEC). **Onça = the financial-services
vertical; Anteater = the sectorial verticals — one codebase, vertical-selected at deploy**,
replacing the fork. This is the general form of ADR-016's Entry-industry scoping and the
per-source env flags.

## Phased plan

1. **Descriptor + derivation (non-breaking).** Add `src/ingest/registry.py` with `SourceSpec` and
   a registry populated for the current 31 sources (descriptive only). Make `candidates.py` derive
   its four lens sets + section list from the registry. No behaviour change; the 773-test suite is
   the guardrail. This alone kills the #50-class "forgot a frozenset" bug.
2. **Pipeline loop.** Replace the hand-coded fetch blocks in `lambda_port` with the registry loop
   (budgets/delta/digest-section uniform). Source-by-source migration behind the same env flags.
3. **Vertical selection.** Add the `vertical` config: `REGISTRY.active(vertical)` gates the source
   set; scope the registry taxonomy + feed to the vertical's industries (generalises ADR-016
   `derive_entry_feed` / tenant scoping). Env flags become spec-derived defaults per vertical.
4. **Unify the fork.** Fold the Anteater sectorial deployment into a vertical config in this repo;
   retire the divergent fork. New source = new file + one `SourceSpec` (auto-registered), zero
   `lambda_port` / `candidates.py` edits.

## Alternatives considered

- **Status quo (hand-wire each source).** Rejected: O(n) boilerplate, four-place lens edits that
  silently mis-score, and no vertical boundary — the pain that prompted this.
- **Separate repo per vertical.** Rejected: this is the current Anteater fork, whose divergence is
  the problem; shared source/lens/governance logic drifts.
- **Full plugin framework (entry-points/setuptools).** Rejected as over-engineered for ~30 in-repo,
  first-party sources; a dataclass registry is enough and stays greppable.

## Consequences & risks

- **+** Adding a source becomes declarative; lens policy is correct-by-construction; verticals are
  explicit and testable; the Anteater fork collapses into config.
- **−** The registry could become a new god-object — mitigated by keeping `SourceSpec` small,
  declarative, and colocated with each module (each module exports its own spec).
- **Risk:** migrating 31 live sources. Mitigated by the phased, **descriptive-first** rollout
  (Phase 1 changes nothing observable) and the existing suite; each phase is independently
  shippable and revertible.
- **Ties:** lens semantics stay owned by the narrative-taxonomy ADR (#34); discovery sources
  (ADR-011) register as specs too; distribution tiers (ADR-016/017) compose with — do not
  duplicate — the vertical scoping.

## Realization status (2026-08-31)

- **Phase 1 SHIPPED** — `src/ingest/registry.py` (`LensSpec` + `SourceSpec`); `candidates.py`
  derives its four lens sets + section list from it (exact reproduction, non-breaking).
- **Phase 2 / 2b SHIPPED** — `_lens_section` (declarative section limits) + `_gated_source`
  (gate→budget→fetch→delta→store) in `lambda_port`. 7 sources registry-driven: regulatory,
  competitor, ofertas, dou, sanctions, cade, contracts. Still-bespoke (side effects / moves /
  special sections): market, new_entrants, pix/juros/inf_diario moves, fatos, sec, fiagro, +
  the non-lens stores (datajud/reclamações/reclame-aqui/consumidor).
- **Phase 3 SHIPPED** — `ONCA_VERTICAL` (default `financial-services`); `_source_enabled`
  vertical-gates the migrated sources' FETCH; a payload post-filter drops the lens SECTION of
  ANY source (incl. still-bespoke FS ones) outside the active vertical. Under a sectorial
  vertical only the `{all}` lens sources (sanctions/cade/contracts) survive.
- **Phase 3b SHIPPED** — `VERTICAL_INDUSTRIES` + `vertical_industries()`; the feed builder
  scopes `feed.json` to the active vertical's industries via `scope_feed_to_modules` (FS =
  `None` = all = no scoping, so Onça is unchanged). Reuses the ADR-016 scoping primitive.
- **Phase 4 SHIPPED (config scaffolding)** — the Anteater sectors (pharma/health/logistics/
  retail/energy/telecom/agro) are first-class recognized verticals (`SECTORIAL_VERTICALS` /
  `KNOWN_VERTICALS` / `is_known_vertical`), each fail-closed (empty industries) until seeded.
  The sectorial product is now a **config** of this codebase, not a code fork.

### Folding a sector fork in (the recipe Phase 4 establishes)

A sector vertical `X` is fully live once:
1. **Taxonomy** — add `X`'s industry slugs to `entity_registry.INDUSTRIES` and to
   `registry.VERTICAL_INDUSTRIES["X"]` (replace the empty fail-closed set).
2. **Entities** — seed `X`'s entity universe into the registry (the fork's data).
3. **Sources** — the sector-agnostic `{all}` sources (CEIS/CNEP, CADE, PNCP) already apply;
   register any sector-specific ingesters as `SourceSpec(..., verticals=frozenset({"X"}))`.
4. **Deploy** — the SAME CDK stack with `ONCA_VERTICAL=X`. The ingest loop runs `X`'s sources,
   and `feed.json` is scoped to `X`'s industries. No code fork.

What remains beyond this repo: the fork's sector ENTITIES + any sector SOURCES are data/specs
to migrate in; the code path is unified. Optional hardening: migrate the still-bespoke FS
fetches to `_gated_source` so a sectorial vertical also SKIPS their fetch cost (today their
output is gated but the fetch still runs); register the non-lens stores as specs.

## What's missing before this is "Implemented"

Honest gap against the Decision above (ranked by how central to the ADR's promise):

1. **The pipeline loop — DONE for the clean sources, remaining for the side-effecting ones.**
   `lambda_port` now runs a real `for spec in registry.active(vertical)` loop over `FETCHERS`
   for regulatory/competitor/ofertas/dou/cade/sanctions/contracts (budget + delta + section
   built in-loop). Still bespoke: the numeric `moves` sources (pix/juros/inf_diario), `market`,
   and the side-effecting lens sources (`sec` content-enrich, `fatos` governance-sort + alias
   accrual, `new_entrants` receita/autocreate) — their in-block side effects / special shapes
   make a clean move risky and are the tracked follow-on.
2. **"Add a source = one spec, zero handler edits" — TRUE for a standard document source**
   (a `SourceSpec` + a `FETCHERS` entry; the section is built in the loop, no payload edit).
   Not yet true for a `moves`/special/side-effecting source.
3. **Per-source config not moved.** `config/watchlist.yaml` still holds the flat FS keyspace
   (`pix_move_threshold_pct`, `dou_lookback_days`, …); the ADR said these move onto the spec.
4. **The fork is not retired.** Phase 4 shipped the *config scaffolding* (verticals recognized,
   fail-closed) but not the substance: the sector industry taxonomy, the seeded sector entity
   universe, and any sector-specific sources still live in the Anteater fork. A sectorial
   vertical publishes an empty feed today, so "one codebase replaces the fork" is enabled, not
   done.
5. **Non-lens stores are outside the model.** datajud, bcb_reclamações, reclame-aqui,
   consumidor, macro, IF.data-store are not `SourceSpec`s, so the registry/vertical model does
   not govern them.
6. **Sectorial vertical is output-gated, not fetch-gated.** Under `ONCA_VERTICAL=<sector>` the
   bespoke FS fetches still RUN (their output is dropped) — correct but wasteful.

To flag **Implemented**: build the real registry loop (items 1–2), move the knobs (3), register
the stores (5) and skip their fetch under a non-FS vertical (6). Item 4 (retire the fork) is a
data migration that also needs the fork's entities/taxonomy — trackable separately.

## Completion scope (from "Implemented (core)" → "Implemented")

The residual is scoped here as concrete, ordered work items — not open-ended "follow-ons".
Each is independently shippable behind the 789-test suite + the handler test (which asserts
the digest), and non-breaking at the default `financial-services` vertical. Two are code in
this repo; one is a data migration in the fork.

| # | Item | Approach | Risk | Done when |
|---|------|----------|------|-----------|
| C1 | **`moves` + `market` into the loop** (pix/juros/inf_diario, market) | Extend the runner with `delta="moves"` (drive `_moves_since_last_run` from spec `key_field`/`value_field`) + a `_moves_section` builder for the `{…_tracked, move_count, items, context}` shape; add `market` as a `delta="none"` store source. Add `_FETCHERS` entries. | Low–med — mechanical; special section shapes, no reordering. | The 5 sources are `_FETCHERS` entries; their bespoke blocks are gone; digest byte-identical. |
| C2 | **Side-effecting lens sources into the loop** (sec, fatos, new_entrants) | Move each fetch into `_FETCHERS`; lift the side effect to a POST-loop step consuming `loop_results[id]`: sec→`enrich_with_content(new)`; fatos→governance-sort of `new` + alias accrual (relocate the accrual block to after the loop); new_entrants→receita enrich + entity autocreate on `new`. | Med — requires side-effect **reordering** on the live path; guard with the handler test per source, one at a time. | The 3 fetches are in the loop; the side effects run post-loop on `loop_results`; digest + registry writes unchanged. |
| C3 | **Config onto the spec** (ADR item 3) | Add a `params: dict` (or typed fields) to `SourceSpec`, populated from `watchlist.yaml`/env keyed by `spec.id`; fetchers read `spec.params` instead of ambient locals. | Low. | No per-source knob is read from a flat `watchlist.yaml` key in `lambda_port`; all via `spec.params`. |
| C4 | **Register the non-lens stores** (item 5) | Allow `lens=None` on `SourceSpec` (exclude from `section_lens_pairs`/`LENSES`); add specs for datajud, bcb_reclamações, reclame-aqui, consumidor, macro, IF.data-store with `integration="store"`/`"macro"` + `verticals`. Gate them via `_source_enabled`. | Low. | Those 6 are `SourceSpec`s and are vertical-gated by the same path. |
| C5 | **Fetch-gate the bespoke sources** (item 6) | Once C1–C2 land, every lens source is in the loop → the loop's `_source_enabled` already fetch-gates. Only the non-lens stores (C4) need the gate added at their block. | Low (subsumed by C1–C2/C4). | Under `ONCA_VERTICAL=<sector>` no FS source runs its fetch. |
| C6 | **Retire the Anteater fork** (item d) | Populate `INDUSTRIES` + `VERTICAL_INDUSTRIES["X"]` for a sector; seed X's entity universe; register any sector `SourceSpec`s (`verticals={X}`); deploy the same stack with `ONCA_VERTICAL=X`. | Med — **data migration**, needs the fork's entities/taxonomy (not in this repo). | A sector deployment runs from this codebase with a non-empty, correctly-scoped feed; the fork is deleted. |

**Sequence:** C1 → C2 (per source) → C3 → C4 → C5 (falls out) — all code in this repo, each a
small PR. C6 is orthogonal (the fork's data) and can proceed in parallel once C1–C5 make the
codebase vertical-complete.

**Definition of Done ("Implemented"):** every source is a `SourceSpec` + `_FETCHERS`/store
entry with no bespoke handler block (C1–C4); adding any source touches only the registry + a
fetcher (no `lambda_port`/`candidates.py`/payload edits); per-source config lives on the spec
(C3); a non-FS vertical runs only its sources' fetches and publishes only its industries
(C5 + Phase 3/3b, already live); and at least one sector runs from this codebase, retiring the
fork (C6).
