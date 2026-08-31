# ADR 019 — Declarative Source Registry & First-Class Verticals

Status: ACCEPTED 2026-08-31 — Phases 1–4 shipped (see "Realization status" at the end; the
still-bespoke FS fetch migration + per-sector taxonomy/entity seeding are the follow-ons).
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
