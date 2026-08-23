# ADR 006 — Beyond SWOT: a framework-parametric belief store (TOWS, Porter, PESTLE, 7S, Four Corners, Ansoff, BCG)

- Status: **TOWS shipped** (2026-08-23); **Porter shipped** (2026-08-23); PESTLE/Ansoff/BCG/Four Corners/7S proposed
- Sourced from **issue #3 — "Add new Strategy Frameworks"** (owner-filed): six
  candidate frameworks scored by adoption / strengths / weaknesses / **OSINT
  feasibility** (TOWS, PESTLE, Porter's Five Forces, SOAR, NOISE, McKinsey 7S).
- **Extended 2026-08-23** with a second tranche beyond issue #3 — **Porter's Four
  Corners, Ansoff Matrix, BCG Growth–Share** (adopted) — plus a survey of further
  frameworks deferred with explicit implementation limitations (§5). These were
  chosen because they cover gaps the first six miss (competitor **prediction**,
  **growth direction**, **portfolio position**) and fit machinery already built
  (`predictive.py`, `operatives.py`, IF.data share, juros pricing).
- Extends [ADR 004](2026-08-22-adr-competitive-thesis-swot.md) (per-entity SWOT
  belief store) and [ADR 003](2026-08-19-adr-narrative-dimensions.md) (the
  `(subject_type, subject_key, axis)` design space). Touches
  [ADR 005](2026-08-23-adr-private-tenant-in-account.md) (a Private tenant's
  private-data lens unlocks the internal-only frameworks).

## Context

ADR 004 gave each tracked competitor a **SWOT belief store**: evidence-linked
S/W/O/T claims, rebuilt each run from deterministic feeders, reconciled by an LLM
stance loop, cold-start-seeded, and promoted to `active` only through analyst
vetting (precision-first — no un-vetted bullet is ever asserted). Issue #3 asks
for *more* strategy lenses on top of that.

The temptation is to build six new subsystems. The **decisive filter** is the
column the issue itself supplies — **OSINT feasibility** — because Onça's entire
moat is *cited, publicly-sourced* intelligence (CLAUDE.md: every synthesized claim
carries a source URL; no login-gated/internal scraping; label estimates). A
framework that needs **internal, aspirational, or subjective** data cannot be fed
from public OSINT without fabricating claims — which this product must not do. So
feasibility, not popularity, decides adoption.

The second realization: most of these frameworks are **not new data problems**.
They are new *shapes* over signal we already ingest, or new *transforms* over
beliefs we already hold. The right move is to **generalize the SWOT store into a
framework-parametric belief store**, not to fork it six ways.

## Decision

### 1. Selection — by OSINT feasibility and machinery fit

| Framework | Subject | OSINT feasibility | Verdict | Why |
|---|---|---|---|---|
| **TOWS Matrix** | per-entity | Medium (a transform of SWOT — free) | **Adopt first** | Not new ingestion: pairs existing **vetted** SWOT bullets into S×O/S×T/W×O/W×T strategic postures; inherits their citations. |
| **Porter's Five Forces** | **sector / industry** | High | **Adopt** | Several forces are *already fed*: rivalry ← IF.data market share concentration; threat of new entrants ← the `new_entrants` discovery lens; substitutes/buyer-power ← thematic axis. |
| **PESTLE** | **market / sector** | High | **Adopt** | Macro & regulatory are our strongest lenses: Legal ← normativos/DOU; Economic ← BCB macro/juros; Political/Tech/Social ← thematic + news. |
| **McKinsey 7S** | per-entity | Medium (structure/systems visible; values/style/skills hidden) | **Partial / defer** | Emit only the **visible** S's (Structure, Systems, Strategy) from filings/press/tech signals, each labeled; **never fabricate** the hidden three. |
| **Porter's Four Corners** | per-entity | Medium (motivation inferred → label) | **Adopt (2nd tranche)** | Converts "what happened" → "what they'll do next"; the reason a CI product exists. Maps onto `predictive.py` + `operatives.py`. |
| **Ansoff Matrix** | per-entity | High | **Adopt (2nd tranche)** | Growth-direction gap the six miss. Classifies each move (penetration / market-dev / product-dev / diversification) by reading filings directly. |
| **BCG Growth–Share** | **sector / segment** | High (quantitative, sourced) | **Adopt (2nd tranche)** | Portfolio position (star/cash-cow/question-mark/dog) from IF.data share × derived growth; dodges the fabrication risk entirely. |
| **SOAR** | per-entity | Low (aspirations/internal results rarely public) | **Reject on public OSINT** | Would force uncited/aspirational claims. Feasible **only** for a Private tenant feeding internal data (ADR 005). |
| **NOISE** | per-entity | Low (internal workshops/subjective) | **Reject on public OSINT** | Same — needs internal operational feedback. Private-tenant-only, if ever. |

**Precision-first stance carries over verbatim:** every adopted framework is
evidence-cited, **proposed-only** until analyst-vetted, and rebuilt each run;
rejected frameworks are rejected *because* they cannot meet the citation bar, not
because they lack value.

### 2. The generalization — one framework-parametric belief store

Reframe `swot_store.py` beliefs to carry a **`framework`** facet, reusing *all* of
ADR 004's machinery unchanged:

```
belief = { framework, subject_type, subject_key, dimension, text,
           evidence[], confidence, status, ... }

  framework ∈ { swot, tows, porter, pestle, mck7s, four_corners, ansoff, bcg }
  dimension ∈ framework-specific set:
    swot         → S | W | O | T                    (unchanged)
    tows         → SO | ST | WO | WT                (paired postures)
    porter       → rivalry | new_entrants | substitutes | buyer_power | supplier_power
    pestle       → political | economic | social | technological | legal | environmental
    mck7s        → structure | systems | strategy   (visible-only; hard-3 omitted)
    four_corners → drivers | assumptions | current_strategy | capabilities
                     (→ a derived response_profile: likely next move, labeled inference)
    ansoff       → penetration | market_dev | product_dev | diversification
    bcg          → star | cash_cow | question_mark | dog     (per segment)
```

Everything ADR 004 already built applies per-framework with **no new subsystem**:
deterministic feeders → beliefs; `swot_reconcile.py` LLM stance + embeddings;
`swot_seed.py` cold-start; `swot/curated.json` durable store (an approved belief
survives each rebuild); the Phase-C vetting UI (Aprovar/Rejeitar → promote/suppress
via `curate.py`). The store stays **recomputable derived state** overwritten each
run; only vetted/curated bullets persist.

**Per-entity** (`subject_type=entity`): `swot`, `tows`, `mck7s`, `four_corners`,
`ansoff`. **Per-sector/segment** (`subject_type ∈ {theme, set, instrument}` from
ADR 003 — reuse the non-entity subjects, do not invent a new axis space):
`porter`, `pestle`, `bcg`. `four_corners` is the one framework that emits a
**derived, forward-looking** bullet (a predicted response) rather than a
present-state claim — it therefore rides the ADR 003 predictive axis' time-gate
and is always labeled inference, never a sourced fact.

### 3. Feeder reuse — new frameworks, mostly existing signal

| New dimension | Fed by (existing) |
|---|---|
| Porter · rivalry | `bcb_ifdata` market-share concentration + `comparative` peer baseline |
| Porter · new_entrants | `bcb_autorizacoes` + `detect_new` (`new_entrants` lens) |
| Porter · substitutes / buyer_power | `thematic` sector currents + news |
| PESTLE · legal | `bcb_normativos` + `dou` (SUSEP/CADE/BACEN/CVM) |
| PESTLE · economic | `bcb_macro` + `bcb_juros` |
| PESTLE · political / social / technological | `thematic` + `trade_press`/news |
| TOWS · SO/ST/WO/WT | the entity's own **vetted** SWOT bullets (transform, no ingest) |
| 7S · structure / systems / strategy | `cvm_ipe` (fatos), IR/press, hiring/tech signals — visible only |
| Four Corners · drivers / assumptions | `operatives` (management/controllers) + `cvm_ipe` (fatos) + IR/news |
| Four Corners · current_strategy | the entity's own vetted SWOT + narrative history (transform) |
| Four Corners · capabilities | `bcb_autorizacoes` (licenses) + `bcb_ifdata` (scale) |
| Four Corners · response_profile | `predictive` axis (derived, time-gated, labeled inference) |
| Ansoff · penetration / market_dev / product_dev / diversification | `cvm_ofertas` + `cvm_ipe` + new fund classes + `bcb_autorizacoes` (new market/license) |
| BCG · star / cash_cow / question_mark / dog | `bcb_ifdata` share (relative share) × `longitudinal` time-series (segment growth) |

TOWS and the LLM-interpretive dimensions run through the **same reconcile/seed
propose→vet path** as SWOT — nothing interpretive auto-asserts.

### 4. The SOAR/NOISE hook (ADR 005)

SOAR and NOISE are rejected **on public OSINT**, not absolutely. A **Private
tenant** (ADR 005) fuses its own internal corpus (deal memos, notes, results) as a
lens — exactly the aspirational/operational data these frameworks need. So they
become a **Private-tenant-only** framework set, still citation-bound (to the
tenant's internal S3 URIs) and still analyst-vetted. This keeps the public product
honest while giving the top tier a differentiated capability.

### 5. Considered but deferred — with their implementation limitation

These were surveyed in the same pass. Each is *deferred, not dismissed*; the note
is the specific reason it does not enter the first two tranches. Adopting any is a
later increment on the same parametric store.

| Framework | Would add | Implementation limitation (why deferred) |
|---|---|---|
| **Porter's Generic Strategies** (cost-leadership / differentiation / focus) | positioning *type* per entity | Cheap once Porter's sector machinery exists, but the classification is **inferential** (pricing from `juros` + segment focus only weakly pin it); risks a low-evidence, one-word label that looks authoritative. Ship as a **derived tag on Porter**, not a standalone framework, once Five Forces is live. |
| **7 Powers** (Helmer — moats: scale, network effects, switching costs, counter-positioning, branding, cornered resource, process power) | durable-advantage analysis, ideal for fintech-vs-incumbent | Only 2–3 powers are OSINT-visible (network effects in Pix, scale from IF.data); the rest need **internal economics** we can't cite. A partial 7 Powers would over-represent the measurable powers and bias the read. Defer until the evidence base is richer; candidate for **Private-tenant** (ADR 005). |
| **Strategy Canvas / ERRC** (Blue Ocean) | visual positioning vs the field | Maps onto the `comparative` axis, but rigorous **competing-factor scoring** from public data is subjective and unstable run-to-run; a shaky canvas reads as false precision. Revisit if a curated factor set + evidence rubric can hold the citation bar. |
| **McKinsey Three Horizons** | innovation/growth over time | Largely **redundant** with the `longitudinal` + `predictive` axes already shipped; adds a label, not a new signal. Fold as a *view* over those axes rather than a framework if ever wanted. |
| **STEEP / STEEPLE** | macro scanning | **Redundant** with PESTLE (same pillars, fewer/more). No new coverage. |
| **VRIO / Resource-Based View** | internal capability/resource moats | Requires **internal** resource & cost data — unsourceable on public OSINT, same bar that rejects SOAR/NOISE. **Private-tenant-only**, if ever. |
| **Balanced Scorecard** | internal KPI/strategy execution | Internal management instrument; no public, per-competitor feed. Out of scope for an OSINT product. |
| **Wardley Mapping** | value-chain evolution/positioning | Needs a modeled value chain + maturity judgement per component; **high analyst effort, low automatability**, weak citations. Not a fit for an automated cited feed. |
| **Jobs-to-be-Done** | customer-demand framing | **Customer-side, not competitor-side** — orthogonal to a competitor-intelligence product; would need primary customer research we don't do. |

The common thread: everything deferred fails on **one** of two counts —
*unsourceable internal data* (7 Powers-partial, VRIO, Balanced Scorecard, and the
Private-tenant set) or *inferential/subjective scoring that can't hold the citation
bar* (Generic Strategies as-standalone, Strategy Canvas, Wardley) — or is simply
*redundant* with an axis already built (Three Horizons, STEEP). None clears the bar
that TOWS/Porter/PESTLE/Ansoff/BCG clear, and none is worth the framework-sprawl
cost yet.

## Consequences

**Positive**
- More strategic lenses with **near-zero new ingestion** — mostly new shapes over
  existing signal; TOWS is pure transform.
- One generalized store, not six subsystems — reconcile/seed/vetting/curation all
  reused; less surface, one precision discipline.
- Sector-level Porter/PESTLE fills a real gap: today's beliefs are per-entity; the
  product had no *industry-structure* view.
- The 2nd tranche fills three more gaps the first six miss — **prediction** (Four
  Corners, on the predictive axis), **growth direction** (Ansoff), and **portfolio
  position** (BCG) — the last two purely from signal already ingested.
- The citation bar becomes the selection criterion — a defensible, on-brand reason
  to reject SOAR/NOISE that also seeds a Private-tenant upsell.

**Costs / risks (honest)**
- **7S is the trap:** the hidden three S's invite fabrication. Guardrail: emit only
  the visible three, each labeled; a reconcile rule must *drop* (not infer) the
  rest. Easy to get wrong.
- More dimensions = more LLM reconcile/seed calls — watch the ~$100/mo ceiling;
  keep cheap-model routing and per-run caps (reuse `ONCA_SEED_MAX_*`).
- Sector subjects need a stable `theme/industry` key space — lean on ADR 003's
  subjects + ADR 002's `IND#` taxonomy; don't mint ad-hoc keys.
- Framework proliferation could clutter the war room; gate new panels behind the
  same vetting UI and show frameworks as tabs, not a wall.

## Alternatives considered
- **Adopt all six as-filed** — rejected; SOAR/NOISE cannot be cited from public
  OSINT and would force hallucinated/aspirational claims (violates CLAUDE.md).
- **Six independent stores/subsystems** — rejected; duplicates ADR 004's
  reconcile/seed/vetting/curation four times over. Parametrize instead.
- **Full McKinsey 7S** — rejected; the hidden S's are unsourceable. Ship the
  visible subset only.
- **Client-side framework toggles over one blob** — rejected; same enforcement/
  clutter reasons as ADR 002's rejected client-side module filter.

## Build deltas (against the current repo)
- `src/synth/swot_store.py` → generalize beliefs with a `framework` facet +
  per-framework dimension sets (keep `swot` behavior identical; additive).
- `src/synth/tows.py` — pure transform over an entity's vetted SWOT bullets →
  SO/ST/WO/WT proposals (propose-only, inherits citations).
- `src/synth/porter.py`, `src/synth/pestle.py` — sector-subject feeders over
  existing lenses (table §3); reconcile/seed reused.
- `src/synth/ansoff.py` — per-entity move classifier over `cvm_ofertas`/`cvm_ipe`/
  fund-class/`autorizacoes` signals into the four growth vectors.
- `src/synth/bcg.py` — per-segment quadrant from `bcb_ifdata` share × derived
  growth (quantitative; no LLM needed for the placement, only the label).
- `src/synth/four_corners.py` — per-entity, emits a **predicted response profile**
  on the `predictive` axis (time-gated, labeled inference); consumes `operatives`
  + the entity's vetted SWOT/history; runs through reconcile like any interpretation.
- `src/synth/swot_reconcile.py` / `swot_seed.py` / `curate.py` — make
  framework-aware (mechanics unchanged; iterate over frameworks).
- Dashboard — framework tabs on the entity/sector panels; proposals ride the
  existing "Propostas pendentes de revisão" vetting path.
- Pipeline order gains the new feeders alongside `swot` (before `feed`).

*Phasing:* **TOWS first** (highest leverage, zero ingestion), then **Porter →
PESTLE** (sector value, existing signal), then **Ansoff → BCG** (sourced,
low-risk, fill the growth/portfolio gap), then **Four Corners** (prediction — high
value but label-heavy, so after the sourced frameworks are stable), then
**7S-visible** (guardrail-heavy). SOAR/NOISE/VRIO and the rest of §5 remain **out**
until either a Private tenant (ADR 005) supplies internal data or the evidence base
clears the citation bar. Closes issue #3 as the design decision; implementation
follows the phasing.

## Implementation notes

> **TOWS shipped (2026-08-23).** `src/synth/tows.py` (`OncaTows` Lambda, wired
> `… → maintenance → tows → threads → …`): reads each entity's active SWOT
> beliefs, synthesizes SO/ST/WO/WT strategic postures via one bounded LLM call per
> entity (propose-only, Phase C vetting). Each TOWS bullet inherits citations from
> the paired SWOT beliefs it draws from. `eligible_entities` requires ≥1 internal
> (S/W) + ≥1 external (O/T) active bullet; `already_proposed` idempotency prevents
> re-drafting; capped at `ONCA_TOWS_MAX_ENTITIES` (8). Store: `tows/proposals.json`
> (idempotent via `swot_reconcile.merge_proposals`). Approved TOWS bullets carry
> `framework: "tows"` in `swot/curated.json`. **Generalization:** `swot_store.py`
> now defines `FRAMEWORK_DIMENSIONS` and `DEFAULT_FRAMEWORK`; `build_beliefs`
> filters curated bullets by framework; `curate._curated_bullet` carries
> `framework` when non-default. Dashboard renders a TOWS panel (`renderTows()`) with
> curated postures + pending proposals, reusing the SWOT card styling with
> quadrant-colored borders (SO=blue, ST=purple, WO=amber, WT=red).
>
> **Porter shipped (2026-08-23).** `src/synth/porter.py` (`OncaPorter` Lambda,
> wired `… → tows → porter → threads → …`): analyzes competitive-structure forces
> (rivalry, new_entrants, substitutes, buyer_power, supplier_power) per entity via
> one bounded LLM call that reads active SWOT beliefs + narrative evidence + industry
> membership from entity_registry. Propose-only, Phase C vetting. `eligible_entities`
> requires ≥3 combined signals (SWOT bullets + narratives); `already_proposed`
> idempotency; capped at `ONCA_PORTER_MAX_ENTITIES` (8). Each assessment carries an
> intensity (high/medium/low) and evidence indices. Store: `porter/proposals.json`.
> Approved Porter bullets carry `framework: "porter"` in `swot/curated.json`. Dashboard
> renders Porter as a collapsible card tab alongside SWOT and TOWS (accordion pattern).
> **UI refactor:** all framework panels (SWOT, TOWS, Porter) now use collapsible card
> tabs (`.fw-card`/`.fw-hdr`/`.fw-body`): header always visible with dimension chips +
> proposal count, click toggles the body, accordion (one open at a time) to avoid
> pollution.
