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

A narrative has three coordinates — **subject_type**, **subject_key**, **axis**.
The *axis* is where the power is, and it is itself a point in a small **design
space** of four knobs:

- **Subject** — entity · pair · person · **set/cohort** · **theme** · **instrument**
  (a norm) · **hub** (infrastructure) · **absence**
- **Temporal stance** — past (descriptive) · present (state) · **future**
  (forecast / deadline)
- **Polarity** — presence-of-signal · **absence-of-expected-signal**
- **Baseline** — self · **peer / cohort** · **expectation** · none

Everything shipped or first-proposed sits in the corner
`{past–present, presence, self-or-pair baseline}`. The unexplored, high-value
territory is **future tense**, **absence polarity**, and **peer / expectation
baselines** — the later axes below are chosen to light those up. Each axis is one
such choice of knobs:

| Axis | subject | unlocks (knob) | what it answers | new machinery |
|---|---|---|---|---|
| **Cross-sectional** *(today)* | entity / signal | — | "what's happening to X now" | — (built) |
| **Longitudinal / trajectory** (#1) | entity | self baseline | "X broke its own pattern" | rolling per-entity **feature store** + anomaly detector |
| **Comparative / peer-cohort** | (entity, peer set) | **peer baseline** | "X is an outlier vs. its cohort" | feature store + **Phase B industries as cohorts** |
| **Cohort / vintage** | a **set** (by entry/attribute) | set + long time | "how the 2024 SCD cohort is faring" | set-longitudinal over registry + feature store |
| **Counterfactual / silence** | an **expected-but-missing** signal | **absence** | "who did *not* act / who went quiet" | **expected-signal (cadence) model** — inverts emit-on-change |
| **Thematic / sector-current** | a **theme** | many-entity subject | "what the sector is collectively doing about X" | topic tagging + cross-entity aggregation |
| **Regulatory-lifecycle / deadline** | a **policy instrument** | **future** (deadline) | "what's due, and who hasn't complied" | instrument thread + deadline extraction + affected-entity map |
| **Behavioral / campaign** (#3) | (entity, pattern template) | pattern | "X is running an event/marketing cadence" | pattern templates / classifier |
| **Threaded / incident** (#2) | an **incident** (event identity) | in-place update | "here's case Y and its latest development" | **event identity** + thread store + `emit-on-update` |
| **Relational / dyadic** (#4, #5) | **entity pair** (+ relation) | pair subject | "A and B are converging / in dispute" | **relationship graph** (typed co-occurrence + evidence) |
| **Operative / person** (§ operatives) | **individual** | person subject | "who is X, where have they been, who do they bridge" | **person nodes** + entity↔person role edges |
| **Ecosystem / dependency** | infra **hub** / directed edge | hub subject | "who's exposed if hub Z breaks" | **dependency ingestion (new source)** + contagion over edges |
| **Predictive / leading-indicator** | (entity, anticipated event) | **future** (forecast) | "what X is likely to do next" | historical pattern→outcome model (**needs data maturity**) |

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

## Operatives — the person layer (larger fulfillment)

Everything above keeps the subject a **company**. But competitive intelligence in
this market is ultimately about **people** — the operatives who carry capability,
intent, and relationships *across* corporate boundaries. The difference is between
"a stealth SCD was authorized" and "the team that built a rival's credit engine
just left to found it, and its QSA names a controller who also sits on a
competitor's board." The human layer is where the edge is, because a company is a
shell around the actors moving through it.

Concretely this makes narratives **projections over a heterogeneous intelligence
graph**, not a feed of company cards — which is the "larger fulfillment": the
product's job is to maintain the graph (entities, operatives, incidents, and the
typed edges among them across time) and *narrate the deltas that matter*.

### Shift 3 — subjects are a heterogeneous graph (entities **+** operatives)

Add a **`person`** node type and **typed, time-bounded** person↔entity edges — the
relationship graph from the relational axis generalizes from entity–entity to a
graph over `{entity, person, incident}`:

- **Role affiliations** (person —role→ entity, with a validity interval): sócio /
  controller (QSA), administrator / director (CVM), founder, board member, legal
  counsel or respondent (DOU / court), regulator. Roles carry *joined/left*, so
  **movement is a first-class fact**, not an inference.
- **This is not greenfield ingestion.** The person seeds are fields we already
  touch: `controllers` / QSA (Receita — already read by `entities.known_parents`),
  the CVM `leader` / `admin` / `manager` fields (already in `signal_blob`), and
  named parties in DOU acts. Shift 3 *promotes those strings into resolved nodes*.
- **People explain edges.** A person node often *grounds* an entity–entity edge
  that would otherwise be speculative: an "A/B convergence" inference becomes a
  **sourced fact** — "the same controller/director bridges them" — rather than a
  guess from co-occurrence. Operatives turn some relational *inferences* into
  citable *facts*.

### Person-enabled axes (extend the taxonomy)

- **Operative profile** (longitudinal on a person): a person's index across
  entities and time — key-person tracking, capability signal.
- **Talent movement** (threaded / longitudinal): a key hire or departure; a whole
  **team lift-out** moving together is a strong new-entrant signal.
- **Interlock / network** (relational *via* a person): shared directors or
  controllers across competitors.
- **Beneficial ownership / who's behind the shell**: extend `known_parents` from
  entity-controllers to *person*-controllers — surface the operative behind a
  quietly-registered SCD.
- **Revolving door**: regulator ↔ industry movement — high-sensitivity in a
  regulated market, and the archetypal person-mediated pattern.

### Person identity — false-nexus, hard mode

Person resolution must be **more conservative** than entity resolution, because
there is no clean anchor: **no CPF as a key** (protected/masked — unlike CNPJ,
which is a public business identifier), rampant **homonyms** (common names), and
name variants (accents, married/maiden, abbreviations). So a person node is only
minted/attached with **corroborating context** (name **+** role **+** affiliating
document), confidence-gated, and **new person nodes go through the step-5 review
queue** — the same precision-over-recall discipline as entities, but people don't
have tickers, so the bar is higher.

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
6. **Add operatives as first-class subjects (Shift 3), public-record-scoped.**
   People are nodes with typed, time-bounded role edges; person resolution is
   review-gated and more conservative than entity resolution. Scope is **strictly
   public professional roles from public records** (QSA, CVM filings, DOU, court
   dockets) — never private-life inference, never a CPF as key.

## Risks / open questions (to steer)

- **Defamation surface (relational).** Dyadic narratives (#4/#5) assert a
  relationship between two real companies. Recall-over-precision here is a legal
  risk, not just a quality one. Likely: relational edges start review-gated and
  never auto-publish an unreviewed "dispute"/"merger" claim.
- **LGPD / privacy (operatives — the hardest guardrail yet).** Person narratives
  are about *named humans*, so **Lei Geral de Proteção de Dados** applies:
  individuals hold data-protection rights that companies do not. Scope to public
  professional roles in public records (a legitimate-interest / public-source
  basis); distinguish a **public figure acting in a corporate capacity** (a named
  controller/director — in scope) from a **private individual** incidentally named
  (out of scope). A false "person X moved to a competitor / is behind shell Y / is
  a litigation respondent" is defamatory about a person — so person narratives are
  review-gated from day one, labeled, cite the record, and never assert unverified
  movement/ownership/litigation. No CPF is stored or keyed on.
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
- **KB grounding horizon vs. retention (S3 Vectors).** The Bedrock KB's *only*
  data source is the **raw** bucket, and a sync runs every ingest — so raw's
  lifecycle expiry *is* the KB's retrievable horizon (deletions are reconciled out
  of the S3 Vectors index within hours, not lagged). The long-lookback axes
  (longitudinal seasonality, YoY comparative, annual-cadence silence) need a full
  prior year to *ground* their strongest claims, so raw retention was raised
  **180 → 395 days** (one YoY cycle + margin). Two design consequences: **(a)**
  build the axis **feature store from `digests/` (durable), never by re-reading
  raw** — so axis *computation* is retention-independent and only *grounding depth*
  is bounded by raw; **(b)** extending retention is **forward-only** — the deep KB
  window refills over ~13 months, while the durable digests keep the analytical
  memory intact meanwhile. Multi-year cohort/vintage is served by durable registry
  facts + digests, not by keeping raw for years.

## Classification — effort, challenge, risk, enabler, value

Effort/risk are relative to *today's* infrastructure (per-entity narratives,
S3 store, registry + industries, review queue, threat scoring, ~14-day window).

| Axis | Effort | Key challenge | Risk | Enabler / dependency | CI value |
|---|---|---|---|---|---|
| Comparative / peer-cohort | **Low** | choosing comparable metrics; normalizing heterogeneous entities | Low | feature store; **cohorts already exist** (Phase B industries) | Med |
| Longitudinal / trajectory | **Low–Med** | thin history (cold-start); threshold tuning vs. false alarms | Low | feature store | Med |
| Cohort / vintage | **Low–Med** | defining dormancy/attrition; needs history | Low | registry + feature store | Med |
| Counterfactual / silence | **Med** | modelling the *expected* signal (cadence); inverts emit-on-change | Low | expected-signal/cadence store (shares feature store) | **High** |
| Thematic / sector-current | **Med** | topic tagging without drift; theme taxonomy | Low–Med | topic classifier (keyword taxonomy → LLM) | **High** |
| Regulatory-lifecycle / deadline | **Med** | deadline extraction from legal text; mapping *affected* entities | Low | instrument thread + LLM extraction | **High** |
| Behavioral / campaign | **Med–High** | defining pattern templates; ambiguous, needs curation/labels | Low–Med | pattern templates / classifier | Med (weakest value/effort for FS) |
| Threaded / incident | **High** | **event identity** (a new false-nexus problem); `emit-on-update` + lifecycle | Med (mis-threading) | thread store | **High** |
| Relational / dyadic | **High** | false edges from coincidence; **defamation** | **High (legal)** | relationship graph | **High** |
| Operative / person — *beachhead* | **Med** | bounded homonyms (keyed by the entrant's filing) | Med | `known_parents` (**exists**) | **High** |
| Operative / person — *full network* | **Highest** | person identity (no CPF, homonyms); **LGPD** | **Highest (legal/PII)** | relationship graph + person nodes | **High** |
| Ecosystem / dependency | **High** | **data gap** — dependency edges rarely in public filings; ingestion is the blocker | Med | **new ingestion source** | **High (but blocked)** |
| Predictive / leading-indicator | **High** | needs labelled history + validation; spurious patterns | **High (inference)** | data maturity + longitudinal history | Highest (**premature**) |

### Others to consider (the leverage points)

- **One investment unlocks four axes: the per-entity feature store** (rolling
  stats + cadence). It is cheap, deterministic, low-risk, and it is the common
  dependency of *longitudinal, comparative, silence, and cohort*. Build it first —
  no user-facing axis, but it de-risks the entire low-risk wave.
- **The thread/edge store** (`emit-on-update` + lifecycle) is the second enabler —
  shared by *incident, regulatory-lifecycle,* and (with the graph) *relational*.
- **The relationship graph** is the third — shared by *relational* and *operatives*.
- **Some axes are gated by inputs, not effort.** *Predictive* is **time-gated**:
  it cannot be forced, it accrues as history accumulates — so treat it as a
  by-product of running the feature store for months, not a scheduled build.
  *Ecosystem* is **source-gated**: the work is finding a dependency-data source,
  not the synthesis.
- **Solve the cross-cutting concerns once, not per axis:** per-axis threat scoring
  (shared 0–1 + factors), dashboard card shapes, and the inference-labeling
  guardrail apply to *every* new axis.
- **Risk clusters at the top of the value column.** The highest-value axes
  (relational, operatives, predictive) carry the legal/PII/inference risk; the
  cheap axes (comparative, longitudinal, cohort) are the safest. Silence,
  thematic, and regulatory-deadline are the sweet spot — **high value, low risk,
  medium effort**.

## Suggested sequencing (waves, not committed)

- **Wave 0 — enabler.** ✅ **Shipped 2026-08-19.** Built the **per-entity feature
  store** (`src/synth/feature_store.py`, rolling stats + cadence). No user-facing
  axis; unlocks Wave 1.
- **Wave 1 — cheap, low-risk, high-leverage.** *Comparative, Longitudinal, Silence,
  Cohort* (all ride the feature store) + *Thematic* and *Regulatory-deadline*
  (independent, high value, low risk). This is the value sweet spot.
  **Silence ✅ shipped 2026-08-19** (`src/synth/silence.py`, `OncaSilence`
  Lambda) as the first detector — proves the pattern: deterministic gate on the
  feature store, LLM-free, labeled inference, its own absence-tuned emit-on-change,
  and a feedback-loop guard (derived axes excluded from feature-store "activity").
  Next: *Longitudinal* (reads `score_z`, already computed) or *Comparative* (reads
  the `cohorts` block) — same enabler, no new machinery.
- **Wave 2 — new stores, moderate risk.** *Threaded / incident* (event identity +
  thread store); the full *Regulatory-lifecycle* thread; *Behavioral*.
- **Wave 3 — graph + legal risk, review-gated from day one.** *Relational*, then
  *Operatives* (beachhead "who's behind this SCD" → full network).
- **Opportunistic / gated.** *Predictive* when history matures; *Ecosystem* if a
  dependency-data source appears.

Independent of Phase C. Nothing here is implemented yet — this ADR records the
model, the classification, and the open decisions for a later build.
