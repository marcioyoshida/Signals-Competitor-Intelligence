# ADR 004 — Competitive thesis: a per-entity SWOT belief store that narratives reinforce or contradict

Status: **Accepted (2026-08-22)** — the model and phased build are committed; **step 1
(Comparative axis) is in progress**. Extends [ADR 003](2026-08-19-adr-narrative-dimensions.md).
Steps 2–4 (the SWOT store, LLM reconcile) remain design-only until their turn.

## Context — where this came from

Deciding the next ADR-003 axis (Comparative / peer-cohort) surfaced a larger idea:
instead of each narrative being an isolated card, every narrative should update a
**durable, structured competitive thesis** for the entity it concerns. The proposal:

- Each tracked entity has a **SWOT file** — Strengths, Weaknesses, Opportunities,
  Threats — as bullets, **stored with embeddings** ("a vector to check against").
- Each time a narrative is shown, it is reconciled against that entity's SWOT and
  either **reinforces** a bullet (corroborating evidence), **contradicts** one
  (counter-evidence → propose a revision), or introduces a **new** bullet.
- The Comparative axis is the first clean *feeder*: "runs hot/cold vs. peers" maps
  directly onto SWOT dimensions with no LLM stance call.

This is the qualitative twin of ADR-003's feature store: the feature store holds
*numbers* (mean/σ/cadence); the SWOT store holds *beliefs* (evidence-linked claims).

## The model

### A per-entity belief store (`swot/{entity}.json`)
Each bullet: `{id, dimension (S|W|O|T), text, confidence (0–1), status
(active|challenged|retired), evidence:[narrative_id…], counter_evidence:[…],
embedding:[…], created, updated}`. A handful to a few dozen bullets per entity — so
"check against" is an **in-process cosine** over the stored embeddings; **no vector
DB needed** at this scale (reuse Bedrock Titan embeddings, or the KB's).

### The reconcile loop (per narrative, after synthesis)
1. Embed the narrative's key claim (one embedding call).
2. Retrieve the **top-k nearest** SWOT bullets for that entity (cosine).
3. **Stance-classify** each near pair (narrative, bullet) → `reinforces |
   contradicts | unrelated` — an NLI task; one bounded LLM call over only the k
   near bullets (not all bullets, not all narratives).
4. Update:
   - *reinforces* → append evidence, bump confidence (with recency weight).
   - *contradicts* → append counter-evidence, lower confidence, mark `challenged`;
     on crossing a threshold **propose** a revision/retirement (review-gated).
   - *no near bullet* → **propose** a new bullet (dimension chosen by the LLM),
     review-gated before it becomes `active`.

Deterministic feeders (Comparative, Longitudinal, Silence) can skip the LLM stance
step — they map to a dimension by rule, attaching as structured evidence.

## How it fits ADR 003 (the review)

- **It is a new tier of Shift 2's derived-state layer.** ADR 003 lists feature store
  → thread store → relationship graph. The SWOT store is a **fourth, qualitative
  tier** (per-entity belief state), built/updated each run like the others.
- **It is a new synthesis *mode*, not a new axis.** Emit-on-change asks "is this
  NEW?"; the SWOT loop asks "does this CONFIRM or CHALLENGE what we believe?" —
  **reconcile-against-belief** generalizes emit-on-change. Axes stay orthogonal:
  every axis's output can land as SWOT evidence.
- **The axes become structured feeders.** Comparative → S/W or O/T by sign;
  Longitudinal → trajectory evidence; Silence → a Weakness/retreat signal; Thematic
  → O/T. So building the axes is not wasted — they are the SWOT's inputs.
- **It reuses ADR 003's guardrail lineage, and needs it more.** SWOT bullets are
  interpretive claims about real named competitors — the same defamation/inference
  surface as the relational axis and the insight card's B/C/D (ADR 003 Decision 4/5).
  Every bullet is **evidence-linked and labeled inference**; a contradiction never
  auto-retires — it **proposes** through the step-5 review queue. Precision-first.
- **It absorbs the roadmap's "comparative dossier / context vector" (#5)** — the
  SWOT *is* the structured dossier — and it **supplies the insight card's
  competitive-context / implication fields (#11 B/C)** from a maintained belief, not
  a per-run guess.

## Classification (ADR 003's rubric)

| Axis | Effort | Challenge | Risk | Enabler | Value |
|---|---|---|---|---|---|
| **SWOT belief store + reconcile** | **High** | stance/NLI accuracy; cold-start seeding; drift/decay; framing | **Med–High** — interpretive claims about named firms (defamation surface) → review-gated, evidence-linked, labeled | embeddings (Titan/KB) + a stance LLM prompt + the review queue | **Very high** — the product's north star: a trustworthy, evolving, cited thesis per competitor ("intelligence, not digest") |

## Hard parts / open questions (to steer)

1. **Frame the SWOT.** Ambiguous by nature. Fix it: **SWOT *of the competitor*** —
   S/W are the competitor's internal attributes, O/T are external market forces
   acting on it. (A separate "threat-to-our-client" view is a per-tenant projection,
   deferred to the accounts/entitlement layer.)
2. **Contradiction detection is the crux and the risk.** NLI over financial claims
   is error-prone; a wrong "contradicts" could retire a true belief. Mitigation:
   contradictions **propose**, never auto-apply; confidence erodes gradually; a
   human vets retirements. This is why the store pairs naturally with **Phase C
   (accounts)** — the vetting UI.
3. **Cold start / seeding.** Options: (a) LLM-draft an initial SWOT from the
   entity's narrative history + dossier, analyst-vetted; (b) pure bottom-up growth
   (slow). Recommend hybrid: drafted, vetted, then evidence-updated. No un-vetted
   bullet is ever asserted.
4. **Drift & staleness.** Beliefs decay; a 2023 Strength may be stale. Confidence
   carries a recency half-life; long-unreinforced bullets auto-`challenged`.
5. **Cost/latency.** Bounded by embed-retrieve-then-judge-top-k (not all-pairs) and
   by only reconciling narratives that actually surfaced. Fits the ~$100/mo envelope.
6. **Feedback loop.** SWOT updates are derived state, **not** activity narratives —
   excluded from the feature store's freshness like the other derived axes.
7. **Perspective on O/T vs the market.** O/T overlap the Thematic axis (sector
   currents). Keep one source of truth: Thematic narratives feed O/T bullets.

## Decision (proposed) & sequencing

**Committed** to the phased path that de-risks; **step 1 building now**:

1. **Ship the Comparative axis first — IN PROGRESS 2026-08-22** (already the next
   ADR-003 item) — standalone value *and* the first structured SWOT feeder. It emits
   a **`swot_hint`** on each card (`dimension: S|W|O|T`, `sign: +/−`, `evidence`) so
   the belief store can consume it verbatim in step 2 — a peer *outperformance* reads
   as a competitor **Strength** (a Threat to our client); *underperformance* as a
   **Weakness** (an Opportunity). No LLM: the sign comes from the peer-z direction.
2. **SWOT store v1 — read-mostly, deterministic feeders only.** Build
   `swot/{entity}.json` + embeddings; let the deterministic axes (comparative,
   longitudinal, silence) attach as evidence. No LLM stance yet. Surfaces a
   per-entity SWOT panel on the dashboard (evidence-linked, labeled inference).
3. **SWOT v2 — reconcile-against-belief (LLM stance).** Add embed-retrieve + stance
   classification for news/fatos narratives; reinforce auto-applies, **contradict &
   new go through the review queue**. Gated on Phase C (accounts) for vetting.
4. **Seeding** (LLM-drafted, analyst-vetted) lands with v2.

Independent of the other roadmap tracks; v2 depends on Phase C. Nothing here is
committed — this ADR records the model, the fit with ADR 003, and the guardrails.
