# ADR — Narrative signal taxonomy: unify axis + lens so frameworks can bind per-dimension

- Status: **Scoped, not decided** (2026-08-26). Deliverable of "scope this" — the
  follow-up that unblocks the deferred half of
  [ADR-006 evidence-independence #32](2026-08-25-adr-framework-evidence-independence.md)
  (the strict per-dimension **axis whitelist**) and sharpens grounding/dashboard filters.
- Builds on [ADR-003 narrative dimensions](2026-08-19-adr-narrative-dimensions.md)
  (the `(subject_type, subject_key, axis)` design space) and the detector pattern.

## Problem (from the #32 audit)

Framework feeders want to bind each dimension to the *evidence axis that actually
evidences it* (Porter: `rivalry←comparative`, `new_entrants←entrants`, …). But the
narrative corpus is **split across two disjoint classification fields**:

- **Derived / belief-axis / detector cards** carry a stamped `axis` — one of
  `comparative, longitudinal, behavioral, regulatory, silence, cohort, relational,
  predictive, ecosystem, thematic, regulatory_lifecycle` (each `<detector>.py` sets
  `AXIS`). ~39 of 227 live cards.
- **Base news narratives** (the majority — **188 of 227**) are produced by
  `synthesize.synthesize_candidate`, which stamps **no `axis`** — they carry `kind`
  + **`lenses`** (`news, pix, regulatory, entrants, juros, fatos, funds, ofertas,
  dou, inf_diario, market, sec`).

So a per-dimension **`axis`-only** whitelist (as #32 decisions 4–5 assumed) drops
~83% of evidence and zeroes out the frameworks. The signal that classifies base
news is `lenses`, not `axis`. Any binding must span **both** fields.

## Two viable designs

**Design B — dual-field gate (framework-local, non-invasive).** Author a
per-framework `DIM_SIGNALS = {dim: (axis_set, lens_set)}`; a cited evidence item
counts for a dimension iff its `axis ∈ axis_set` **or** any of its `lenses ∈
lens_set`. No change to how narratives are stamped; no reprocessing. Directly
unblocks #32's deferred half. Reversible, unit-testable.

**Design A — unified `topic` field (corpus-level, substrate).** Stamp a coarse
`topic`/`signal_class` on **every** narrative at synth time (derive from `axis` when
present, else from `lenses`+`kind` via a `LENS_TO_TOPIC` map), so one field
classifies the whole corpus. Frameworks (and the agent, dashboard filters,
grounding) then read a single field. Bigger change: touches `synthesize_candidate`,
the feed schema, and wants a backfill of historical narratives.

**Recommendation: phase it.** Ship **Design B first** — it unblocks #32 now at low
risk and *proves the dimension→signal mapping* against live data. Promote to
**Design A** once the mapping is validated, because a unified `topic` also sharpens
(a) agent grounding (`select_grounding` could boost on-topic cards), (b) dashboard
lens/axis filters, and (c) future frameworks — value beyond #32.

## Core artifact — proposed dimension → signal map (first cut, to refine)

`axis` values in **SMALL-CAPS-ish**, lenses in `code`. Porter's row is the only one
that exists today (in its docstring); the other five are authored here and must be
tuned against a live dry-run (see Validation).

| Framework | dimension | axes | lenses |
|---|---|---|---|
| **Porter** | rivalry | comparative | `market` |
| | new_entrants | — | `entrants` |
| | substitutes | thematic | `pix`,`news` |
| | buyer_power | thematic | `news`,`pix` |
| | supplier_power | regulatory | `juros`,`regulatory`,`dou` |
| **PESTLE** | political | regulatory | `dou`,`regulatory` |
| | economic | — | `juros`,`market` |
| | social | thematic | `news` |
| | technological | thematic | `pix` |
| | legal | regulatory, regulatory_lifecycle | `dou`,`regulatory` |
| | environmental | thematic | `news` (ESG — sparse) |
| **Ansoff** | penetration | comparative | `market`,`pix` |
| | market_dev | — | `entrants`,`news` |
| | product_dev | — | `ofertas`,`fatos`,`pix` |
| | diversification | ecosystem | `fatos` |
| **BCG** | star | comparative | `market` |
| | cash_cow | — | `market`,`juros` |
| | question_mark | thematic | `entrants` |
| | dog | silence, longitudinal | — |
| **Four Corners** | drivers | behavioral | `fatos` |
| | assumptions | thematic | `news` |
| | current_strategy | comparative | `ofertas` |
| | capabilities | — | `market`,`funds`,`inf_diario` |
| | response_profile | predictive, behavioral | — |
| **7S-visible** | structure | — | `fatos`,`dou` |
| | systems | — | `pix`,`inf_diario`,`funds` |
| | strategy | comparative | `ofertas`,`fatos` |

Rows with an empty axis/lens column bind on the other alone; a dimension with an
empty gate (should be none after tuning) would fall back to "any cited evidence" so
it never silently emits zero — the Validation step catches over-restrictive rows.

## Work breakdown (Design B / Phase 1)

1. **`framework_common.py` (new, small):** `DIM_SIGNALS` type + `on_signal_ids(cited,
   evidence, dim, dim_signals)` → the subset of cited evidence ids whose axis/lens
   matches the dimension. One shared helper (the ADR-006 "shared gate" intent).
2. **Per feeder (6):** add the framework's `DIM_SIGNALS` map; in `analyze_*`, replace
   the current `ev_ids = [evidence[j]["id"] …]` with the on-signal filter and keep
   the existing `if not ev_ids: continue` hard drop (now axis/lens-valid). Ensure
   `_collect_evidence_ids` carries `lenses` (today it carries only `axis`).
3. **Tests:** per feeder — a cited off-signal narrative is dropped; an on-lens news
   narrative is kept; a dimension still emits when it has real on-signal evidence.
4. **Validation dry-run (gates rollout):** run the pipeline once, log per-dimension
   kept-vs-dropped counts; tune any row that drops to ~zero. THEN enable the gate.
5. Update #32 §Implementation reality → "axis/lens binding shipped"; close #32.

## Work breakdown (Design A / Phase 2, follow-up)

6. `synthesize_candidate`: add `topic` (derive from `axis`|`lenses`+`kind` via
   `LENS_TO_TOPIC`); thread through `feed_builder` into feed cards.
7. One-time backfill stamping `topic` on historical `narratives/` (or accept
   forward-only). 8. Point `DIM_SIGNALS` at the single `topic` field. 9. Optional:
   `agent_ask.select_grounding` on-topic boost; dashboard topic filter.

## Effort / risk

- **Phase 1: ~S/M, low-moderate risk.** 1 new helper + 6 mechanical edits + tests;
  the Validation dry-run is the safety gate (prevents the #32 zero-out). Reversible
  (a feeder env flag can bypass the gate).
- **Phase 2: ~M, moderate risk.** Schema addition + backfill; broader blast radius
  (feed/agent/dashboard) but each piece is additive.

## Open decisions

1. **Phase 1 only, or commit to A now?** (Recommend Phase 1 first.)
2. **Empty-gate fallback:** a dimension with no matching evidence — *drop the
   dimension* (strict anti-fabrication, #32's intent) vs *fall back to any cited
   evidence* (keeps coverage). Recommend **drop**, with the Validation dry-run
   ensuring no row is chronically starved.
3. **Own the mapping as code or curation?** Start as code constants (`DIM_SIGNALS`);
   promote to registry/config later only if analysts need to retune without deploy.
