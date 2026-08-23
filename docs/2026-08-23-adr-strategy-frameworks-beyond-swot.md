# ADR 006 — Beyond SWOT: a framework-parametric belief store (TOWS, Porter, PESTLE, 7S)

- Status: **Proposed** (2026-08-23)
- Sourced from **issue #3 — "Add new Strategy Frameworks"** (owner-filed): six
  candidate frameworks scored by adoption / strengths / weaknesses / **OSINT
  feasibility** (TOWS, PESTLE, Porter's Five Forces, SOAR, NOISE, McKinsey 7S).
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

  framework ∈ { swot, tows, porter, pestle, mck7s }
  dimension ∈ framework-specific set:
    swot   → S | W | O | T                    (unchanged)
    tows   → SO | ST | WO | WT                (paired postures)
    porter → rivalry | new_entrants | substitutes | buyer_power | supplier_power
    pestle → political | economic | social | technological | legal | environmental
    mck7s  → structure | systems | strategy   (visible-only; hard-3 omitted)
```

Everything ADR 004 already built applies per-framework with **no new subsystem**:
deterministic feeders → beliefs; `swot_reconcile.py` LLM stance + embeddings;
`swot_seed.py` cold-start; `swot/curated.json` durable store (an approved belief
survives each rebuild); the Phase-C vetting UI (Aprovar/Rejeitar → promote/suppress
via `curate.py`). The store stays **recomputable derived state** overwritten each
run; only vetted/curated bullets persist.

`SWOT` and `TOWS` and `7S` are **per-entity** (`subject_type=entity`); `Porter`
and `PESTLE` are **per-sector/theme** (`subject_type ∈ {theme, set, instrument}`
from ADR 003 — reuse the non-entity subjects, do not invent a new axis space).

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

TOWS and the LLM-interpretive dimensions run through the **same reconcile/seed
propose→vet path** as SWOT — nothing interpretive auto-asserts.

### 4. The SOAR/NOISE hook (ADR 005)

SOAR and NOISE are rejected **on public OSINT**, not absolutely. A **Private
tenant** (ADR 005) fuses its own internal corpus (deal memos, notes, results) as a
lens — exactly the aspirational/operational data these frameworks need. So they
become a **Private-tenant-only** framework set, still citation-bound (to the
tenant's internal S3 URIs) and still analyst-vetted. This keeps the public product
honest while giving the top tier a differentiated capability.

## Consequences

**Positive**
- More strategic lenses with **near-zero new ingestion** — mostly new shapes over
  existing signal; TOWS is pure transform.
- One generalized store, not six subsystems — reconcile/seed/vetting/curation all
  reused; less surface, one precision discipline.
- Sector-level Porter/PESTLE fills a real gap: today's beliefs are per-entity; the
  product had no *industry-structure* view.
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
- `src/synth/swot_reconcile.py` / `swot_seed.py` / `curate.py` — make
  framework-aware (mechanics unchanged; iterate over frameworks).
- Dashboard — framework tabs on the entity/sector panels; proposals ride the
  existing "Propostas pendentes de revisão" vetting path.
- Pipeline order gains the new feeders alongside `swot` (before `feed`).

*Phasing:* **TOWS first** (highest leverage, zero ingestion), then **Porter →
PESTLE** (sector value, existing signal), then **7S-visible** (guardrail-heavy).
SOAR/NOISE remain **out** until a Private tenant (ADR 005) supplies internal data.
Closes issue #3 as the design decision; implementation follows the phasing.
