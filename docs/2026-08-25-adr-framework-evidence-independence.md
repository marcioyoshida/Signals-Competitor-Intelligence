# ADR 006 — Addendum: framework evidence-independence (own-track evidence, TOWS exempt)

- Status: **Partially SHIPPED** (2026-08-25, `<pending>`). Decisions 1–3 built &
  deployed; decisions 4–5 (the strict per-axis whitelist) **deferred** — see the
  *Implementation reality* section: live narratives are ~83% `axis: None`, so a
  strict axis whitelist would drop the bulk of framework evidence and collapse
  proposals to near-zero. Records a backlog decision surfaced
  while reviewing the parallelization work (issue #10) — see
  [pipeline-parallel note] and the base ADR
  [2026-08-23-adr-strategy-frameworks-beyond-swot.md](2026-08-23-adr-strategy-frameworks-beyond-swot.md).
- Owner-raised framing: *"For better framework analysis, do not derive from SWOT.
  Each framework stack should have its own track. With thin evidence, don't
  fabricate an assertion yet. Each assertion must be followed by an evidence link."*
- Supersedes nothing shipped; tightens the drafting contract of the six non-TOWS
  framework feeders. No store/schema migration.

## Context

ADR 006 generalized SWOT into a framework-parametric belief store: eight frameworks
draft → (auto-)approve → promote, sharing one curated store. Each feeder
(`src/synth/<fw>.py`) was *intended* to draw its dimensions from the axis that
actually evidences them — Porter's own docstring maps `rivalry→comparative`,
`new_entrants→BCB authorizations lens`, `substitutes→thematic`,
`supplier_power→regulatory`, etc. In practice three seams still couple every
framework's output to the **SWOT belief store** rather than to its own evidence:

1. **Eligibility counts SWOT as evidence.** `porter.select_entities`
   (`src/synth/porter.py`, mirrored in the other feeders) gates on
   `n_active_swot + n_narratives >= min_evidence`. An entity with ≥3 active SWOT
   bullets but **zero fresh narratives** still qualifies — so a Porter/PESTLE draft
   can be produced with no framework-native evidence at all.
2. **SWOT bullets enter the drafting prompt as citable-looking context.**
   `_draft_prompt` prints "Current SWOT beliefs about this competitor" directly
   above "Recent signals (evidence)". The model can restate a SWOT bullet as a
   framework assessment and attach a loosely-related evidence index.
3. **The evidence-link gate is presence-only, not axis-checked.** `_parse_draft`
   accepts any evidence index that is *in range*; "omit a force with no supporting
   evidence" is a soft prompt instruction, not a hard drop. Nothing checks that the
   cited narrative is actually on that dimension's declared axis.

This is exactly the fabrication risk the product forbids: a framework assertion
that reads as sourced but is really an echo of SWOT.

## Decision

**Six frameworks get their own evidence track; TOWS stays SWOT-derived; thin
evidence emits nothing; every assertion carries an axis-valid evidence link.**

1. **Own-track eligibility (Porter, PESTLE, Ansoff, BCG, Four Corners, 7S).**
   Change the quorum to **framework-native evidence only** — `n_narratives >=
   min_evidence` (corpus/narrative signal on the framework's own axes). Active SWOT
   bullets no longer contribute to the quorum.
2. **SWOT demoted to non-citable context (same six).** SWOT bullets may still be
   passed to the drafter as *background* but must be clearly non-citable — an
   assessment may cite **only** narrative/corpus evidence ids, never a SWOT bullet.
   (Simplest safe implementation: drop SWOT bullets from the six non-TOWS prompts
   entirely and let the belief axes speak through the narrative evidence they
   already produced.)
3. **TOWS is the deliberate exception.** TOWS is *definitionally* a transform of
   SWOT quadrants (SO/ST/WO/WT). Its evidence link **is** the parent SWOT bullet
   id(s), which are themselves evidence-linked to narratives. TOWS keeps deriving
   from the SWOT store; the rule below is satisfied transitively.
4. **Hard per-axis quorum (anti-fabrication).** A dimension/force emits **nothing**
   unless it has ≥1 narrative *on its declared axis*. Move "omit with no evidence"
   from a prompt suggestion to a hard drop in `_parse_draft`.
5. **Strict, axis-valid evidence link.** Reject any assessment whose evidence
   indices don't resolve to narratives on that dimension's axis, rather than
   accepting any in-range index. Every surviving assertion therefore carries at
   least one axis-valid citation.

## Non-goals / explicitly rejected

- **Do not fork the feeders.** The ADR-006 win was the parametric store. This is a
  *parametrization* change (evidence-source per framework) plus a stricter shared
  `_parse_draft` gate — reused across the six, TOWS exempt. No new subsystems.
- **No new ingestion.** "Own track" means *own evidence axis within the existing
  corpus*, not a new source. The belief axes already produce the per-dimension
  signal; the fix is to stop letting SWOT stand in for it.
- **No schema/store migration.** Proposal/curated records are unchanged; only the
  eligibility gate, the prompt, and the parse-drop rule move.

## Consequences

- Fewer, better-grounded framework proposals: entities with SWOT coverage but no
  fresh framework-axis signal will (correctly) produce **no** Porter/PESTLE/etc.
  bullet that run, instead of a SWOT echo. Expect proposal counts to drop and
  citation quality to rise.
- TOWS coverage is unaffected (still parametric over vetted SWOT).
- The auto-approval + confidence-heatmap path is unchanged; it operates on whatever
  survives the stricter gate.

## Implementation sketch (for the tracking issue)

- `select_entities`: quorum `n_narr >= min_evidence` (drop `n_active_swot` term) in
  the six non-TOWS feeders.
- `_draft_prompt`: remove the SWOT-beliefs block (or mark non-citable) in the six.
- `_parse_draft`: hard-drop any assessment with no evidence index **or** whose
  indices are not on the dimension's axis; add a per-feeder `DIM_AXES` map
  (already implicit in each docstring).
- Tests: assert (a) an entity with SWOT-only, zero-narrative input yields zero
  proposals; (b) an assessment citing an off-axis narrative is dropped; (c) TOWS
  still derives from vetted SWOT.

## Implementation reality (2026-08-25)

**Shipped (decisions 1–3):** the six non-TOWS feeders (`porter`/`pestle`/`ansoff`/
`bcg`/`four_corners`/`seven_s`) now gate eligibility on **narrative evidence only**
(`eligible_entities`: `n_narr >= min_evidence`; the `n_active_swot` term is gone),
and the **SWOT-beliefs block was removed from every drafting prompt** — the model
can now draw ONLY on the cited narrative evidence, not on SWOT bullets. Combined
with the pre-existing hard "no evidence index → drop" gate in `_parse_draft` +
`analyze_*` (`if not ev_ids: continue`), this delivers the *"don't derive from
SWOT"* decoupling and the *"every assertion carries an evidence link"* floor. TOWS
is untouched (it stays a SWOT transform, by design).

**Deferred (decisions 4–5, the strict per-axis whitelist):** an audit of the live
feed found **188 of 227 narratives carry `axis: None`** — only belief-axis/detector
cards are axis-tagged, while the bulk (plain news, the *richest* framework evidence)
have no axis. A strict "a dimension emits nothing without ≥1 narrative on its
declared axis" rule would therefore drop ~83% of evidence and zero out the
frameworks. The densely-populated field is `lenses` (news/pix/regulatory/entrants/
juros/fatos/…), not `axis`. So the axis whitelist is **not buildable as specced
against current data**; it needs either (a) narrative **axis enrichment**, or (b)
reconceiving the per-dimension binding around `lenses`. Tracked as a follow-up on
issue #32; NOT shipped to avoid breaking the frameworks.

## Open questions

- Should SWOT bullets remain visible to the drafter as *non-citable* background
  (richer prompts, small echo risk) or be removed entirely (strictest)? Leaning
  **remove** for the first cut.
- Is a single `min_evidence` right per framework, or should axis-sparse frameworks
  (7S-visible, Four Corners) get their own floor? Defer until we see the
  post-change proposal counts.
