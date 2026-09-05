# ADR 021 — Executive Flow, per-officer sectorial dashboards & the decision→KB expertise flywheel

- Status: **PROPOSED** — 2026-09-04. Owner-requested ("the dashboards are running thin — make
  them sectorial, with CSO/CPO/CRO/CCO collecting decisions and inference into the KB; the
  reference should be CORS-forwarded to collect metrics").
- **Extends** [ADR 020](2026-09-04-adr-write-agent-api-officers.md) — the write-capable Agent
  API (`/api/act`) + the four specialist officer agents, **Phases 1–3 already SHIPPED + LIVE**
  (typed catalog, elevated authz, idempotency, journaled to `OncaCurationLog`,
  propose-vs-apply, the four officers `src/dashboard/officers.py`, the chief-of-staff router
  `officers.route`, owner-based hand-off). ADR-020 is not superseded; this ADR builds the
  **delivery + decision-learning layer** on top of it.
- **Adapts** the sister product's ADR — *Officer Agents, Executive Flow & Decision-Trust
  Metrics* (`Signals-Sectorial-Intelligence`, 2026-09-04) — back into Onça. That ADR itself
  adapted Onça's ADR-020; this closes the loop, bringing its **§D Executive Flow, §E
  Decision-Trust metrics, §F expertise flywheel, §G per-officer sectorial dashboards, and §H
  reference panels + decision→KB loop + CORS followed-link beacon** to Onça's financial-services
  domain.
- Builds on (Onça today — **most of the sister's "prerequisites" are already shipped here**):
  the read Agent API (`agent_ask.py`, `/api/ask` — now officer-persona aware, ADR-020 §2); the
  write Agent API (`act_api.py`, `/api/act`); **ADR-018 FULLY REALIZED** (per-field provenance +
  the append-only **`OncaCurationLog`** + write-precedence + the `integrity.py` detectors +
  rollback over the journal + `scripts/curation_admin.py`) — i.e. Onça already has the audit +
  rollback the sister listed as an un-built Phase 0; **Cognito identity + the Phase D per-tenant
  read boundary** (`onca-tenant-config`, the JWT authorizer, `scope_feed_to_modules`); the
  card-level `industries` denormalization + `_card_industries` + `feed.groups` (sector scoping is
  a filter, not a rebuild); the incident **thread** store (`src/synth/threads.py`, ADR-003 Wave 2
  — the Trajectory base); the Bedrock **Knowledge Base** + S3-Vectors RAG path (`ONCA_KB_ID`,
  `agent_ask._kb_retrieve`, `retrieve.py`, `start_ingestion_job`); the 8 strategy frameworks
  (ADR-006), the ameaça×expansão position map (ADR — visual competitive mapping), regulatory-change
  intelligence (ADR-009: `reg_change`/`reg_diff`/`reg_change_record` + `reg_coverage`), distress
  (ADR-012), the coverage-gap loop (ADR-014, `gaps_api.py`), entity discovery + radar-score (#14),
  and the reputation/financials stores.

## Context

Onça's officer layer already **thinks and acts** — `/api/ask` answers grounded/cited questions
through any of four officer personas, and `/api/act` lets an officer emit its bounded, audited
catalog actions with router + hand-off. What is *thin* is the **delivery surface**: the v2
dashboards are per-sector read screens (`/adquirencia`, `/fintech`, `/seguros`, `/wealth`) and an
operator `/admin` rail, but there is **no per-officer executive surface**, no **decision-feedback
capture**, and no loop that turns decisions into sharper officers. The officers are invisible to
the buyer, and the highest-value data the product could produce — *which executive decisions were
taken, on what evidence, with what outcome* — is not captured at all.

Three forces shape this ADR (mirroring the sister, tightened to Onça's head-start):

1. **The write + audit + officer primitives already exist and are LIVE.** ADR-020 shipped the
   `/api/act` contract and the four officers; ADR-018 shipped the append-only journal + rollback +
   integrity detectors. We are not building a mutation surface or an audit trail from scratch — we
   are building the **executive delivery + the decision loop** on top of primitives already trusted
   in production.
2. **Sector scoping is already a solved filter.** `_card_industries` + `entity_attrs.industries` +
   `scope_feed_to_modules` (the Phase D tenant boundary) already slice the feed by industry. A
   per-officer, industry-scoped dashboard is a **projection**, not a rebuild.
3. **The owner wants the decisions captured and fed back into the KB.** The value thesis is not
   "an officer dashboard exists" but "every briefing shown, action recommended, decision taken,
   source consulted, and outcome observed becomes a labelled example that makes the officers more
   expert." The dashboard is the officers' classroom; the KB is where the lesson is stored.

## Decision

Add four layers on top of ADR-020: **(D)** an **Executive Flow** that turns a threat/fact/incident
into an executive briefing + recommended actions and captures the decision taken; **(E)**
**Decision-Trust metrics** (ETS/TDR) computed from that capture; **(F)** an **expertise flywheel**
that promotes closed decisions-with-outcomes into a per-officer experience corpus embedded in the
KB and retrieved during officer planning; **(G)** **per-officer, industry-scoped dashboards**
(CSO/CRO/CCO/CPO switchable in one app) that are also the capture surface; and **(H)**
**reference/consultation panels for all four officers** + the continuous **decision→KB loop** + a
**CORS followed-link beacon** (`/api/beacon`) that attaches the consulted-source evidence trail to
each decision. Reads stay `/api/ask` (officer-persona aware); writes and captures stay on the
typed `/api/act` catalog + the audit journal — no arbitrary-mutation surface is introduced.

### D. Executive Flow — incident-driven orchestration & the decision loop

The officers exist to move a **threat, fact, or incident** all the way to an **executive decision**
and to *learn from it*:

```text
Registry → Sector Intelligence → Executive Correlation Engine
        → Executive Briefing → Recommendations → Trust & Decision Metrics
```

- **Trigger → Trajectory.** A qualifying event opens a durable **Trajectory** on the existing
  `threads.py` store: a new high-threat narrative, an ADR-009 reg change with market blast-radius,
  a distress/integrity/sanction hit, or a material competitor move (`feed.groups` / momentum).
- **Executive Correlation Engine.** The **chief-of-staff router already shipped in ADR-020**
  (`officers.route` + `officers.owner_of`) classifies the trigger and dispatches to the relevant
  officer(s); officers **hand off** as in ADR-020 (the Regulator routes a market-wide change to
  Compliance). Every dispatch + hand-off is already journaled to `OncaCurationLog`.
- **Executive Briefing + Recommendations.** The officer(s) produce a coordinated, **cited** brief
  (reusing the `/api/ask` grounded/persona path) and **recommended actions across horizons —
  Imediato / 30 dias / 90 dias / Horizonte estratégico** — each recommendation carrying its
  grounding and, where it maps to a catalog action, a one-click **propose** into `/api/act`.
- **Decision-feedback capture → `OncaDecisionLog`.** When the executive acts on (or dismisses) a
  recommendation, the decision + rationale + (later) realized outcome are captured into a **new
  append-only `OncaDecisionLog`** table (the decision-outcome analogue that sits beside the
  ADR-018 `OncaCurationLog` action journal). This is the loop that powers §E/§F.

**Executive Dashboard** (the CSO delivery surface, §G): Situação (Strategic Climate Index,
manchetes executivas, sinais emergentes, resumo 7 dias) · Riscos & Oportunidades (radar de
oportunidade/risco, matriz de prioridade) · Inteligência competitiva (momentum, movimentos
estratégicos, sinais de M&A, ganhadores/perdedores) · Inteligência regulatória (nível de ameaça,
linha do tempo, análise de impacto) · Ações recomendadas (Imediato/30d/90d/Estratégico).

> **Purpose of the Executive Dashboard.** It is not only a delivery surface — its purpose is to
> **collect the metrics and realized outcomes of executive decisions as training INPUT** for the
> KB that makes the officers more expert over time (§F). Every briefing shown, action recommended,
> decision taken, and outcome observed is a labelled example. The dashboard is the officers' classroom.

### E. Decision-Trust metrics (the success definition)

Computed from `OncaDecisionLog` + engagement telemetry; surfaced to operator/tenant.

**Executive Trust Score (ETS)** — trust to act:

```text
ETS = 0.40·Feedback + 0.25·Decision Influence + 0.20·Executive Engagement + 0.15·Board Adoption
    0–6.0 Baixa confiança · 6.1–7.5 Útil · 7.6–8.5 Confiável · 8.6–10 Ativo estratégico
```

**Time-to-Decision Reduction (TDR):** `TDR = (Before − After) / Before × 100` (e.g. 16h → 4h ⇒ 75%).

**Primary KPIs:** Executive Trust Score · Decision Influence Rate · Time-to-Decision Reduction ·
Executive Hours Saved · Board Adoption Rate. **Explicitly NOT primary (vanity):** logins, page
views, prompt volume, MAU. TDR baselines are recorded per tenant, never assumed.

### F. The masterplan — the decision corpus & the expertise flywheel

**Decisions + their outcomes become the officers' training corpus.** Every closed Trajectory yields
a labelled example — `{trigger, briefing, recommended actions, decision taken, rationale, consulted
sources (§H beacon), realized outcome, metrics (ETS contribution / TDR / influence)}` — the
highest-value proprietary data the product produces. This closes the loop *knowledge → decision →
outcome → sharper knowledge*, per officer domain.

- **The Experience Corpus.** `OncaDecisionLog` entries, once an outcome is observed, are promoted
  into a durable, per-officer **experience store** and embedded into the Bedrock KB (S3 Vectors) as
  first-class **cited** documents, alongside the raw market/regulatory corpus the officers already
  ground on. A decision-with-outcome is a *precedent*.
- **Mechanism 1 — Retrieval-augmented expertise (primary, ship first).** An officer planning a new
  Trajectory retrieves its own past decisions **and outcomes** for similar triggers (reusing
  `agent_ask._kb_retrieve` / the existing grounded-cited RAG path — no model weights change, every
  claim keeps provenance). Outcomes are weighted (a recommendation that led to a good outcome is a
  stronger precedent).
- **Mechanism 2 — Offline distillation / fine-tuning (later, optional).** Once the corpus is large
  enough, the labelled `(context → action → outcome)` set can distil into per-officer policies
  (few-shot libraries first; a fine-tune only if it clears an eval bar against held-out outcomes).
  Never in the request path; always offline, versioned, evaluated before promotion.
- **The metrics ARE the labels.** ETS/TDR/Decision-Influence are the **reward signal** labelling
  which recommendations to reinforce — which is *why* the primary KPIs are decision-outcomes, not
  usage vanity: usage can't train an expert; outcomes can.
- **Guardrails on learning.** Outcome labels are provenance-stamped (ADR-018) and human-correctable;
  training/retrieval never overrides the no-fabrication + citation contract; **tenant experience is
  isolated by default** (one tenant's decisions never train another's officers) with cross-tenant
  learning only on explicit, anonymised opt-in; LGPD/defamation guardrails apply to stored decisions.

### G. Per-officer sectorial dashboards — industry-scoped, capture-for-inference

The full product is **four per-officer dashboards**, bound by two cross-cutting rules:

- **Industry scoping (first-class).** Every officer dashboard has an **industry selector** (Onça's
  financial sectors — bancos, fintechs, adquirência, seguros, wealth/asset-mgmt, previdência
  fechada, securitização, consórcio, fundos agro/imobiliários, crypto, apostas + "todas"); choosing
  a sector filters *every panel* to that industry's data, reusing the card-level `industries`
  denormalization + `_card_industries` + `scope_feed_to_modules` (the same machinery as the
  tenant/entry slices) — a filter, not a rebuild.
- **Capture-for-inference (first-class).** Every officer dashboard is a **data-generation surface**:
  each panel's proposals/recommendations carry **aprovar / rejeitar** + an **outcome** field; each
  interaction is journaled to `OncaCurationLog` (actor = officer + industry + identity) and, once an
  outcome is observed, promoted into the per-officer experience corpus (§F) and rolled up into
  per-officer-per-industry metrics (§E). This is how the dashboards "map actions for future
  inference": approvals + outcomes are the labels that train each officer in *its* expertise, *per
  sector*.

**Panel matrix (Onça — all four officers ground on data already present):**

- **CSO — Strategic** (the §D Executive Dashboard): Situação · Riscos & Oportunidades · Inteligência
  competitiva · Regulatória (resumo) · Ações recomendadas. Grounds on the ameaça×expansão map, the 8
  frameworks, threat/momentum, `feed.groups`, financials + reputation. Actions: curar tese
  (review-gated `curate_belief`), acionar pipeline (`trigger_run`), abrir watch estratégico
  (`open_watch`).
- **CRO — Regulator**: Nível de ameaça regulatória · Linha do tempo (consulta→norma→fiscalização) ·
  Análise de impacto / blast-radius (ADR-009 `reg_change_record`) · Prazos/vigências. Actions: watch
  em instrumento (`open_watch`), acionar pipeline (`trigger_run`), (roadmap) ack/rotear mudança.
- **CCO — Compliance**: Auditoria de integridade (ADR-018 detectors + remediation) · Registro de
  risco (distress ADR-012, reputação-como-risco, sanções CEIS-CNEP / antitruste CADE onde ingeridos)
  · Triagem de watchlist (hits por indústria) · Log de curadoria + **rollback**. Actions: rodar
  auditoria (`run_integrity_audit`), reverter escrita ruim (`rollback_field`/`revert_entity`),
  sinalizar hit (`flag_entity`).
- **CPO — Product**: Mapa de cobertura + Pontos cegos (ADR-014 `gaps_api`) · Propostas de descoberta
  + radar-score (#14) · Portfólio de fontes / novas verticais (ADR-019) · Encaixe JTBD. Actions:
  triar lacuna, aprovar/rejeitar proposta (`resolve_review`), propor vertical (`propose_vertical`).

**Delivery.** One **v3 executive app** with an **officer switcher** (CSO/CRO/CCO/CPO) + the
**industry selector**, over per-officer, industry-scoped `feed.executive.{officer}` blocks built by
a new `src/synth/executive.py` (shared render, per-officer builders) — *not* four separate sites, so
scoping, capture and metrics stay uniform. Reuses the v2 shell (`app.js`/`ask.js`, clean-route +
dir-index CloudFront function). v3 ships CSO first; CRO/CCO/CPO read-panels are the next slice.

### H. Reference/consultation panels + the decision → KB feedback loop + the CORS beacon

- **Reference/consultation panels for ALL four officers.** Extend the cold-start *referência*
  baseline (ADR-014 seeding) beyond CRO (regulatory reference) + CPO (source catalog) to **CSO**
  (playbook estratégico: the 8 frameworks + the ameaça×expansão axes) and **CCO** (playbook de
  compliance: the risk taxonomy — distress/sanções/antitruste — + the ADR-018 governance model +
  the LGPD/defamation/attribution guardrails). Every officer leads with structure for consultation
  even at zero live signal. Labelled *referência* (base) — never a live/dated or company-specific
  claim. All four also seed the KB.

- **Decision → KB feedback loop (the ultimate goal).** Beyond the one-time cold-start seed, a
  **continuous, incremental promotion** turns closed decisions (decision + outcome + the §H
  consulted-link evidence trail) from `OncaDecisionLog` into **experience-corpus KB documents** (raw
  bucket → `start_ingestion_job`) each pipeline cycle, **seen-set-gated** (reusing the ADR-023
  news-style deferred-commit discipline) so only new decisions are promoted. Realizes §F Mechanism 1:
  an officer's next briefing grounds on its own past decisions + outcomes. Instrument every capture
  point liberally.

- **Followed-link capture (CORS beacon) — decision context.** When an executive opens a
  source/citation link *within a decision context*, a fire-and-forget **beacon**
  (`navigator.sendBeacon` / `keepalive` fetch → **`POST /api/beacon`**, CORS-enabled) records
  `{context_id, officer, industry, url, ts}`. The beacon appends the followed links to that
  decision's context in `OncaDecisionLog`, so the **evidence trail** (which sources informed the
  decision) becomes part of the decision's KB document — a far richer training label than the bare
  verdict, and the raw material for engagement/influence metrics (§E). **Guardrails:**
  operator/consented + fail-closed authz (same origin-secret / Phase-D identity path as the other
  `/api/*` writes); **no PII**; best-effort (a dropped beacon never blocks navigation); the captured
  URL is a first-party citation link already shown in the UI, not arbitrary browsing. This is the
  literal meaning of "the reference is CORS-forwarded to collect metrics."

## Guardrails (product discipline — unchanged, extended to the executive layer)

- Fail-closed authorization; per-tier (or operator-secret) action allowlist; writes/captures require
  an elevated capability. Auto-apply ONLY when reversible + idempotent + low-blast; everything else
  proposed (ADR-020). Every action + dispatch + hand-off + decision + beacon is journaled (actor,
  before/after where applicable, outcome).
- No fabrication; briefings and recommendations are grounded/cited like `/api/ask`. High-stakes
  actions are never silent — proposed, or elevated + audited.
- Officers emit **catalog** actions only (ADR-020 `_CATALOG`) — a fixed allowlist, not open text.
- Cost/rate bounds on Bedrock planning + correlation; idempotency keys prevent double-application.
  LGPD/defamation guardrails apply to all officer output and to stored decisions.
- **Metrics honesty:** ETS/TDR are decision-outcome measures, not usage vanity; baselines are
  recorded per tenant, not assumed. The beacon captures first-party citation clicks only, no PII.

## Consequences

- A delivery + decision-learning layer that makes the (already-shipped) officers **visible and
  measurable** to specialized, senior, regulated financial-services buyers — re-anchoring success on
  **decisions**, not usage.
- New to build (Onça): the append-only **`OncaDecisionLog`** (decision + outcome) beside the existing
  `OncaCurationLog`; **`src/synth/executive.py`** (`feed.executive.{officer}` per-officer/per-sector
  blocks); the **v3 executive app** (officer switcher + industry selector + capture controls); the
  **`/api/beacon`** endpoint; and the continuous **decision→KB promotion** job. Onça **skips the
  sister's Phase 0** — the audit journal, rollback, integrity detectors, KB, identity/tenant boundary
  and the officers/router/hand-off all already exist.
- More Bedrock cost (per-officer planning + correlation + experience-corpus embedding) — bounded; the
  read path is unchanged.
- **The defensible asset (masterplan, §F):** the decision-with-outcome **experience corpus** is the
  product's compounding moat — proprietary, per-officer, per-sector, improving with every executive
  decision. It is *why* the primary KPIs are decision-outcomes (the training labels), not usage vanity.

## Status / phasing

PROPOSED. Onça already holds ADR-020 (Phases 1–3) + ADR-018 + Cognito/Phase D + the KB + `threads.py`,
so phasing starts at the **dashboards**, not the plumbing.

**Parallel read track (no backend — dashboards first, the owner's ask):** the §G per-officer,
industry-scoped read-panels ship ahead of the capture backend, over `feed.executive.{officer}` built
from data already present, with the **aprovar/rejeitar + outcome** capture controls rendered
**disabled** until the decision backend lands.

1. **`OncaDecisionLog` + `/api/act` decision intents** — the append-only decision-outcome store +
   `record_decision` / `set_outcome` catalog actions (auto-apply: append-only, low-blast), journaled
   like every other `/api/act` call. (Onça's ADR-018 audit + rollback already exist — no Phase 0.)
2. **`executive.py` + the v3 app** — the officer switcher + industry selector + the per-officer
   `feed.executive.{officer}` blocks; CSO Executive Dashboard first (§D), then CRO/CCO/CPO panels.
3. **Executive Flow** — Trajectory on `threads.py` + the Executive Correlation Engine (reuse
   `officers.route` + hand-off) + the Briefing / Recommended-Actions surface + decision capture
   (Phase 1) wired to the recommendations.
4. **Decision-Trust metrics** — ETS / TDR / the primary-KPI panel on the Executive Dashboard,
   per-officer-per-sector rollups from `OncaDecisionLog`.
5. **Reference for all officers + decision→KB loop + the CORS beacon (§H)** — extend the *referência*
   baseline to CSO + CCO; add the continuous seen-set-gated **decision→KB promotion**; add
   **`/api/beacon`** (fail-closed, no-PII, best-effort) attaching the consulted-source evidence trail
   to each decision. "Capture decisions and feed the KB" becomes continuous here.
6. **The expertise flywheel (§F)** — retrieval-augmented expertise (Mechanism 1) into officer
   planning first; offline distillation (Mechanism 2) only later, behind an outcome-eval bar;
   tenant-isolated by default.

## Core principle

```text
Registry delivers knowledge.
Sector packs deliver context.
Officers (CSO/CRO/CCO/CPO) deliver perspective.
Executive Flow delivers decisions.
Decisions + outcomes retrain the officers — the flywheel compounds expertise.
```

Related: extends [ADR 020](2026-09-04-adr-write-agent-api-officers.md) (write-agent + officers) and
the read-only ADR 010 (`/api/ask`); reuses ADR-018 (audit/rollback/integrity), ADR-002 Phase D
(identity/tenant), ADR-003 Wave 2 (thread store), ADR-009 (reg-change), ADR-012 (distress), ADR-014
(coverage-gap + cold-start reference), ADR-006 (frameworks), ADR-019 (source registry). Mirrors the
sister ADR in `Signals-Sectorial-Intelligence` (regulated-sectors domain). Issue: #20 follow-on.
```
