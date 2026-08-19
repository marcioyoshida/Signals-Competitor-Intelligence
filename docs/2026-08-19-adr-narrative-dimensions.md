# ADR 003 — Narrative dimensions: longitudinal, threaded, and relational synthesis

- Status: **Proposed** (2026-08-19)
- Builds on [ADR 001](2026-08-17-adr-entities-registry.md) (entity identity /
  registry) and [ADR 002](2026-08-18-adr-commercial-multitenancy.md) (industry
  modules / entitlement). Independent of Phase C (Cognito) — this is a
  **synthesis-layer** evolution, not an access-control one.

## Context

Today a narrative is a **cross-sectional** object: it fuses *many sources at one
point in time* about *one subject*. Concretely the synthesis unit is keyed
**(subject, run_date)** where subject is either an entity or a single signal:

- `cand-ent-{entity_id}` (`kind=entity_fusion`) — clusters an entity's signals
  **within a single run** (`_cluster_by_entity` → `_candidate_from_cluster`).
- `cand-{short}` (`kind=regulatory[_fusion]` / `competitor:{lens}`) — a single
  signal (+ soft-related backdrop).

`emit-on-change` (`_has_change`: some source is `is_new`/a threshold move) drops
steady-state restatements, and the object is written to
`narratives/{run_date}/{id}.json`. **All cross-time structure exists only
downstream**: `feed_builder` assembles a per-entity sparkline (peak score + count
per date) for the dashboard. Nothing in *synthesis* reasons across dates or
across entities — the model never asks "did X break its own pattern?" or "are A
and B converging?" It only ever asks "what is true about X in this run?"

The five seed examples the product owner raised are precisely the narratives that
**cannot be expressed as (entity, run_date)**:

1. **Pattern-break index** — an entity deviates from its own multi-date baseline.
2. **Incident update** — a fraud case discovered, then *updated* as it develops.
3. **Marketing/operation pattern** — a recurring campaign/event cadence.
4. **Merger likelihood** — two entities with converging projects/signals.
5. **Legal dispute between entities** — a typed relationship, A vs. B.

(The list is explicitly open — the taxonomy below is meant to absorb new axes.)

## The underlying reframe

A narrative has three coordinates, of which we currently vary only the first two
poorly: **subject_type**, **subject_key**, **axis** (temporal shape). Generalize:

| Axis | subject | temporal scope | what it answers | new machinery |
|---|---|---|---|---|
| **Cross-sectional** *(today)* | entity / signal | one run | "what's happening to X now" | — (built) |
| **Longitudinal / trajectory** (#1) | entity | rolling window | "X broke its own pattern" | rolling per-entity **feature store** + anomaly detector |
| **Threaded / incident** (#2) | an **incident** (event with identity) | open-ended, **updated in place** | "here's case Y and its latest development" | **event identity** + thread store + `emit-on-update` |
| **Behavioral / campaign** (#3) | (entity, pattern template) | window | "X is running an event/marketing cadence" | pattern templates / classifier |
| **Relational / dyadic** (#4, #5) | **entity pair** (+ relation type) | window | "A and B are converging / in dispute" | **relationship graph** (typed co-occurrence + evidence) |

Two structural shifts unify every row:

### Shift 1 — Narrative identity generalizes beyond (subject, run_date)

Threads and relationship edges are **living documents**: a fraud incident or an
A–B dispute persists and is *updated* on new evidence — not re-emitted as an
independent daily card. So:

- The store keys these by a **stable subject key independent of run_date**
  (`INCIDENT#<id>`, `REL#<a>#<b>#<relation>`, `TRAJ#<entity>#<metric>`), with
  run_date recorded as *when last updated*, not as the partition.
- `emit-on-change` gains a sibling **`emit-on-update`**: append a development to
  an existing thread/edge rather than mint a new card.
- The **cumulative-store discipline extends** (the recurring lesson: the
  narrative store never prunes; stale artifacts must be *explicitly* superseded).
  Threads/edges need an explicit lifecycle — `open → developing → resolved/closed`
  — or they accumulate forever. "Never prune" becomes "never prune, but *close*."

### Shift 2 — A derived-state layer between raw signals and synthesis

Today synth reads *this run's* digest. Longitudinal, threaded, behavioral, and
relational narratives need **history as input**. Introduce a materialized layer,
rebuilt/updated each run *before* synthesis:

- **Per-entity rolling features** — baselines (mean/σ, cadence) so a break is
  detectable deterministically (cheap; no LLM).
- **Incident/thread store** — open cases, so a new signal can be *attached* to an
  existing thread instead of starting one.
- **Relationship graph** — typed edges accumulated from co-occurrence + explicit
  evidence (a shared offering, a joint DOU act, a lawsuit filing).

This is the largest engineering change: synthesis becomes **multi-producer** —
several detectors, each keyed differently, each with its own scoring + grounding
rules, all feeding the one feed. It mirrors the existing **candidate → synthesize**
split: cheap deterministic detectors *nominate*; the LLM only *narrates* the few
that pass the gate (keeps cost inside the ~$100/mo envelope).

## Decisions (proposed)

1. **Adopt the axis taxonomy** above as the narrative-type model; carry
   `subject_type` + `axis` on every narrative so the feed and filters can face it.
2. **Generalize narrative identity** to a stable subject key; add `emit-on-update`
   and an explicit thread/edge **lifecycle** (`open/developing/closed`) alongside
   `emit-on-change`. Cumulative store still never silently prunes.
3. **Build a derived-state layer** (feature store → thread store → relationship
   graph) as a pipeline step before synthesis; deterministic nominators, LLM only
   for narration.
4. **Grounding: inference is labeled, never asserted as fact.** Merger likelihood,
   dispute inference, and pattern-break significance are *derived estimates*, not
   sourced claims. They must (a) be visibly flagged as inference (as the threat
   score already is "heurística, não modelo validado"), (b) cite the *underlying*
   signals as evidence, (c) **never state a merger/dispute as fact**. This extends
   the existing guardrail lineage (`scrub_fake_url_tokens`, the AUM floor).
5. **Identity is the false-nexus risk, again — reuse the discipline.** Threading
   two unrelated frauds into one case, or asserting an A–B edge from coincidental
   co-occurrence, is the substring-identity bug's relational cousin — and a false
   "A and B in legal dispute" is *higher-stakes than Stone/Rolling Stone*
   (potentially defamatory about two real named companies). New threads and new
   edges are created **precision-first**, through a **confidence gate + the step-5
   review queue**, exactly like new entities earn `news_safe`.

## Risks / open questions (to steer)

- **Defamation surface (relational).** Dyadic narratives (#4/#5) assert a
  relationship between two real companies. Recall-over-precision here is a legal
  risk, not just a quality one. Likely: relational edges start review-gated and
  never auto-publish an unreviewed "dispute"/"merger" claim.
- **Per-axis threat scoring.** Each axis needs its own semantics (a break is
  inherently high-novelty; a dyadic score blends both entities' salience +
  evidence strength) while staying on the shared 0–1 + factor breakdown.
- **Entitlement (ties to ADR 002).** A relational narrative spans two entities
  that may sit in *different* industry modules — which module "owns" the A–B
  edge for entitlement? (Defer to Phase D, but the identity model must not
  preclude a clean answer.)
- **Dashboard shapes.** New card types: threaded incident (update timeline),
  relationship edge (A—B with relation label), trajectory break (sparkline with
  the anomaly marked). The single-entity card generalizes; filters gain an
  axis/type facet.
- **Cost/latency.** A stateful pre-synthesis step + multiple producers multiply
  work; hold the line with deterministic nomination, LLM only on gated survivors.

## Suggested sequencing (dependency order, not committed)

1. **Longitudinal / trajectory (#1)** and **behavioral (#3)** — need only
   per-entity history; buildable on the existing single-entity store. Lowest risk,
   no new identity problem. *Start here.*
2. **Threaded / incident (#2)** — introduces event identity + the thread store +
   `emit-on-update`; the first mutable-document narrative.
3. **Relational / dyadic (#4, #5)** — needs the relationship graph and carries the
   defamation risk; heaviest and last. Review-gated from day one.

Independent of Phase C. Nothing here is implemented yet — this ADR records the
model and the open decisions for a later build.
