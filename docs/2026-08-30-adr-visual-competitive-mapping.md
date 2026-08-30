# ADR 015 — Visual competitive mapping: Mapa Competitivo (Threat × Momentum) and the warroom visual layer

- Status: **Proposed** (2026-08-30). Design decision; implementation follows the
  phasing in §6.
- Sourced from an owner request to add a **Gartner-style Magic Quadrant** (Vision ×
  Ability to Execute, market-leader dots) to the warroom, and to explore other
  **high-assertion, high-visual-impact** graphs for the dashboard.
- Extends [ADR 003](2026-08-19-adr-narrative-dimensions.md) (derived-state layer),
  [ADR 004](2026-08-22-adr-competitive-thesis-swot.md) (SWOT belief store),
  [ADR 006](2026-08-23-adr-strategy-frameworks-beyond-swot.md) (the citation-bar
  selection filter that also governs which *visuals* we can honestly render),
  [ADR 012](2026-08-25-adr-distress-rj-store.md) (distress store), and the
  Phase 3 dashboard (`src/dashboard/site/index.html`, `feed_builder.py`).

## Context

A Magic Quadrant is the canonical "one-glance competitive landscape" artifact:
a 2-axis scatter (X = Ability to Execute, Y = Completeness of Vision) with named
market leaders plotted as dots into four quadrants. The request is reasonable —
this audience skims, and a single defensible picture of the field is worth more to
them than another scroll of cards. The question is whether *this* artifact, from
*this* corpus, clears the bar the rest of the product is held to.

Two independent reads (corpus audit + buyer-side positioning review) converged on
the same answer, so it is recorded here rather than relitigated.

### Why a Gartner Magic Quadrant, specifically, is rejected

The MQ **name and layout** are rejected on three independent counts. This is not a
"can't build it yet" — it is "don't build this shape."

1. **The Vision axis is unsourceable — and rendering it detonates the moat.**
   "Completeness of Vision" is aspirational/subjective by construction. Onça's
   entire differentiator is *cited, publicly-sourced* claims (CLAUDE.md: every
   synthesized claim carries a source URL; label estimates). An AI-assigned
   coordinate placing *Itaú at 3.4 on Vision* is precisely the fabricated,
   false-precision claim the product forbids — and it invites the "cite your
   source" objection **on our own flagship artifact**, in front of the exact
   regulated buyer we built the citation engine for. ADR 006 already rejected
   SOAR/NOISE/VRIO for the identical reason (they "force uncited/aspirational
   claims that cannot hold the citation bar"); a Vision axis fails the same test.

2. **Wrong question.** A Gartner MQ answers *"which vendor should I buy?"* — a
   procurement aid whose value is the named analyst's defended authorship. Onça's
   entities are not vendors the buyer evaluates to purchase; they are
   **competitors the buyer defends against**. The MQ's implicit verb is "select";
   the buyer's verb is "anticipate/counter." Importing the layout imports the
   wrong decision frame.

3. **Trademark is a hard distribution blocker, not cosmetics.** "Magic Quadrant"
   and "Leaders / Challengers / Visionaries / Niche Players" are enforced Gartner
   marks. A regulated buyer's own legal/compliance will not let a knock-off MQ
   circulate internally — the same governance rigor that makes them our ideal buyer
   makes them reject it. That kills internal distribution, which is the artifact's
   only value.

### Corpus reality (why even a re-labeled MQ isn't feasible today)

Even setting the name aside, the data cannot carry independent, defensible axes
across the competitor set:

- **X — Execute** is genuinely sourceable, but only for a narrow, bank-skewed
  population: CVM financials (`src/ingest/cvm_financials.py`, keyed by `entity_id`)
  give hard revenue/assets/margin for ~6–10 domestic listed banks. IF.data market
  share exists (`src/ingest/bcb_ifdata.py`) but is **keyed by raw institution name
  and never joined to `ENT#`** — no `CodInst`/name→`entity_id` resolver is
  persisted anywhere. The fintech challengers — the interesting part of any
  Brazilian-fintech quadrant — have **no execution numbers** (foreign-listed ones
  file at the SEC not CVM; private ones file nothing).
- **Y — Vision** has **no sourced signal at all**. The only per-entity material is
  LLM-drafted, analyst-vetted SWOT/framework belief counts — usable as a coarse
  ordinal, but every number is interpretive and would need an "estimated" label.

So the set of entities with real data on *both* axes is small and made of
incumbents — a proxy-heavy 2×2 of listed banks, not a defensible quadrant of the
field. **Verdict: buildable-but-proxy-heavy, and only for a narrow population — not
worth building as an MQ.**

## Decision

**Do not build a Magic Quadrant. Build a cited `Mapa Competitivo` (Threat ×
Momentum) as the primary landscape artifact, and surface the already-emitted-but-
unrendered corpus signals as a small set of high-assertion warroom visuals.**

The selection rule is ADR 006's, applied to *pictures* instead of frameworks:
**a visual may render only axes/encodings that are sourced, or explicitly labeled
as an estimate/count.** No visual gets to imply a precision the data doesn't have.

### 1. Mapa Competitivo — Threat × Momentum (the MQ replacement)

A cited bubble-scatter, the same "one glance, who's coming for my book" job, with
axes that are honest:

- **X = expansion momentum** — a **count** (not a fabricated 0–100 index) of
  expansion-lens narratives per entity over the trailing window
  (`new_entrants`/autorizações, `ofertas`, new fund classes/`inf_diario`, Pix).
  Auto-scaled per render, axis labeled with real units
  ("sinais de expansão · janela de N dias"). It's our fastest-cadence signal, which
  fits the payments/fintech beachhead.
- **Y = threat-to-us** — reuse `entities[].peak_score` (already the basis of every
  Cobertura heat cell), inverted in SVG space (top = high threat) so it matches the
  `TIERS` mental model users already have.
- **Bubble size = market share** — sqrt-scaled radius (area accuracy), clamped
  `r ∈ [6,28]px`, from IF.data. Entities with no resolved share get a fixed small
  **dashed-outline** dot + "sem dados IF.data" — never an invented size.
- **Color/pattern = distress state** — neutral fill by default; distressed entities
  (present in `distress[]`) get a dashed red stroke (`--t-crit`) + a "⚠ RJ" /
  "⚠ falência" text badge. Distress is a genuine threat semantic, so red is
  appropriate here — but color is **never the only encoding** (dash + badge +
  tooltip + legend all carry it; colour-blind-safe).
- **Quadrant guides** — dashed lines at the *existing* tier threshold (0.65 on Y)
  and the momentum median on X, with **plain descriptive Portuguese** labels
  ("entrante em aceleração", "ameaça estabelecida, baixo movimento", "avanço
  agressivo", "baixa prioridade") — deliberately **not** Leader/Challenger/
  Visionary/Niche, per the trademark and framing rejection above.

**Placement:** a top-level `.panel` between the KPI tile row and the "Perguntar à
Onça" panel — a primary strategic view, not a secondary rail drawer. **Chart type:**
inline SVG, hand-rolled like the existing `sparkline()` — buildless, no library.
**Interaction:** clicking a bubble sets `state.entity` and calls the existing
`syncEntityHighlight()`/`render()` path — so the feed below re-filters to that
entity's narratives, each already carrying its numbered clickable citation footer.
Source drill-down is therefore **free** (one line of reuse), which is the whole
point: the map is a lens over the cited feed, never an authored verdict.
**Theme:** reuse existing CSS vars only (`--accent`/`--t-crit`/`--border`/`--ink`/
`--muted`/`--panel`); entity labels render *adjacent* to bubbles (on the flat panel
ground), never on the translucent fill, so AA contrast holds in both themes.

Governed by the same **derived, recomputable, propose→analyst-vet** discipline as
BCG/Porter — the map is a projection over state, rebuilt each run.

### 2. The other warroom visuals (ranked, by leverage ÷ cost)

Two load-bearing findings shaped this ranking:
- **`distress[]`, `reputation[]`, and `financials[]` already ship in `feed.json`
  today and are rendered nowhere** (grep-confirmed) — free, high-value inventory.
- **There is no persisted per-entity market-share or momentum number** — both are
  real new backend fields, not UI work (see §3).

| # | Visual | Shows | Feeds (existing unless noted) | Build cost |
|---|---|---|---|---|
| 1 | **Radar de Risco** | ranked fusion of BCB complaints rank + distress state + threat tier | `reputation[]` (official BCB rank), `distress[]`, `peak_score` | **Zero backend** — data already ships, currently invisible |
| 2 | **Balanço de Crença Estratégica** | per-entity diverging bar: S+O (teal) vs W+T (red), ranked by net pressure | `swot[entity].counts` (ADR-004) | Zero backend; must keep the `inferência` label — it's a belief count, not a fact count |
| 3 | **Radar Regulatório / Calendário de Prazos** | contributions-style calendar heatmap of `regulacao`-topic volume + upcoming deadline pins | `topics` (regulacao), reg-lifecycle `deadline`/`days_to_deadline` (ADR-003) | Zero backend |
| 4 | **Ranking de Momentum** | expansion-velocity leaderboard (bar list) | **new** `entities[].momentum` (from §3) | Rides §3's momentum store; near-zero after it lands |
| 5 | **Linha do tempo de ameaça** | multi-entity comparative threat timeline (small-multiples / stacked area) | `entities[].timeline` (already powers per-row sparkline) | Zero backend, but most front-end code — ranked last on that basis |

**Deprioritized (explicitly):**
- **Treemap de concentração setorial** — needs the same new market-share store as
  §1's Mapa *and* non-trivial layout code, and is largely redundant with
  bubble-size-by-share. Revisit only on a specific sector-concentration ask.
- **"Strategic-group map" from Porter/PESTLE belief-axis counts** — turning sparse
  qualitative *mention counts* into two continuous named axes ("foco tecnológico")
  reads as a quantitative model it isn't; high risk against the no-unlabeled-proxy
  rule, and the SWOT diverging bar (#2) conveys similar information more honestly.
  Would need its own ADR to justify the framing before building.

### 3. New backend fields required (the only non-UI work)

Both are small, durable, recomputable stores in the ADR-012 `distress/index.json`
shape — loaded by `feed_builder.py`, emitted onto `entities[]`:

- **`entities[].momentum`** — a per-entity weighted count over the expansion lenses
  (`new_entrants`/`ofertas`/`funds`/`inf_diario`/`pix`), analogous to the existing
  `build_industry_volume()` but per-entity. A **count**, never a 0–1 index.
- **`entities[].market_share_pct`** (nullable) — resolve `bcb_ifdata.market_share()`
  output from raw institution *name* to a registry `entity_id` via the same
  resolver `bcb_reclamacoes.map_to_entities` already uses, persist it, and emit it.
  This is the piece that unlocks a genuinely-sourced size encoding for incumbents.

### 4. Honest-labeling guardrails (non-negotiable)

- **Momentum v1 covers only sources that exist.** The brief named *hiring velocity*
  as an input, but there is **no hiring/LinkedIn ingester** (CLAUDE.md: LinkedIn
  data only via a licensed aggregator, not built). The axis label must name only
  autorizações/ofertas/funds/Pix — never imply hiring is included until PDL/
  Explorium ships.
- **No invented sizes/positions.** Missing market share → dashed fixed dot +
  "sem dados IF.data". Missing threat → not plotted. Never a fabricated coordinate.
- **Belief counts stay labeled inference** (#2), consistent with the `.infer`
  convention already in the UI.
- **Sparse-window empty state.** Few entities with both axes → explicit
  "dados insuficientes para o mapa neste período", not 1–2 floating dots that read
  as the whole field.

## Consequences

**Positive**
- The primary landscape artifact is **fully cited** (every axis sourced or labeled)
  and drills to filings — it *shows off* the moat instead of undermining it, which
  the MQ would have done.
- Four of six visuals are **zero-backend** — `distress`/`reputation`/`financials`/
  `swot counts` are already in `feed.json` and currently wasted; #1–#3 are pure
  front-end wins.
- The `market_share_pct` resolver is independently valuable — it's the same
  IF.data→`ENT#` join the roadmap already wants (2026-08-22 review), unlocking
  future sector/BCG work, not just this chart.
- Surfaces the SWOT belief store (#2) as a single glanceable visual — selling the
  product's differentiated IP, not raw feed volume.

**Costs / risks (honest)**
- Until `market_share_pct` resolution ships, most Mapa bubbles show "sem dados
  IF.data" (small dots) — **sequence the momentum + share stores before shipping
  the Mapa**, or ship with an honest interim caption that IF.data coverage is
  partial. The zero-backend visuals (#1–#3) should land first regardless.
- A new top-of-page panel affects all warroom users; keep it collapsible if it
  crowds the skim.
- More per-entity numbers to compute per run — both are cheap deterministic counts/
  joins (no LLM), so the ~$100/mo ceiling and pipeline timing are unaffected.

## Alternatives considered
- **Build the Magic Quadrant as requested** — rejected; unsourceable Vision axis
  (moat-detonating), wrong decision frame, trademark distribution blocker, and no
  corpus data for defensible axes across the field (§Context).
- **Re-labeled 2×2 of listed incumbents only** (X = CVM revenue, Y = vetted SWOT-
  opportunity count, all marked "estimated") — technically honest but low value:
  bank-skewed, blank for the fintech challengers, and Y still purely interpretive.
  Deferred behind the sourced Threat × Momentum map, which covers the whole tracked
  set.
- **Porter strategic-group map** (the legitimate MQ cousin, structural axes, no
  trademark) — a real candidate for concentrated niches (banking/IB), but gated on
  a stable structural-axis definition per niche; kept in the deprioritized list
  (§2) pending its own framing ADR.
- **BCG (already shipped, `src/synth/bcg.py`)** — the honest sourced quadrant
  (share × growth) already exists; the Mapa complements it (threat/momentum, not
  portfolio position) rather than duplicating it. Do not rebuild BCG with worse
  axes.

## Build deltas (against the current repo)
- `src/dashboard/feed_builder.py` — emit `entities[].momentum` (per-entity
  expansion-lens weighted count) and `entities[].market_share_pct` (nullable, from
  the resolved+persisted IF.data store).
- `src/ingest/bcb_ifdata.py` + a new small durable store — resolve
  `market_share()` names to `entity_id` (reuse `bcb_reclamacoes.map_to_entities`)
  and persist, same shape as `distress/index.json`.
- `src/dashboard/site/index.html` — the Mapa Competitivo panel (inline SVG bubble
  scatter, reusing `sparkline()`'s SVG idiom, `#tip` tooltip, `state.entity` drill-
  down) + the five visuals in §2 (all reusing existing CSS vars and filter state).
- No pipeline-order change for the zero-backend visuals; the momentum/share stores
  compute inside the existing feed-build step (no new Lambda).

*Phasing:* **#1 Radar de Risco → #2 Balanço de Crença → #3 Radar Regulatório**
(all zero-backend, immediately visible) → **momentum + market-share stores (§3)** →
**Mapa Competitivo + #4 Ranking de Momentum** (ship together) → **#5 timeline**
(last, most front-end code). Closes the Magic-Quadrant request as this design
decision.
