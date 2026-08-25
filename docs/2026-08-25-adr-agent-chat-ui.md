# ADR 010 — Agent Chat in the dashboard: a curated, grounded Q&A over the tool's own data

- Status: **Implemented (v1, read-only)** — 2026-08-25. Owner-requested.
  `src/dashboard/agent_ask.py` (`OncaAgent` Lambda, `/api/ask/`) + the dashboard
  "Perguntar" panel + `tests/test_agent_ask.py`. Grounds on the published
  `feed.json` (scoped selective retrieval) + KB Retrieve; scope gate + grounded-only
  citation contract; deployed behind the same edge auth. A write-capable Agent API
  (#20) remains out of scope.
- Delivers the UI + read-only inference behind **issue #21 (AI searchbar — infer against
  the database)** and a first read-only surface of **issue #20 (Agent API)**.
- Builds on: the dashboard's **`/api/*` = Lambda Function URL + CloudFront behavior**
  pattern (`/api/run/`, `/api/registry/*`, `/api/review`), the **Bedrock Knowledge Base**
  over narratives (`ONCA_KB_ID`, `bedrock:Retrieve`), the entities registry
  ([ADR 001](2026-08-17-adr-entities-registry.md)), and the SWOT/framework belief store
  ([ADR 004](2026-08-22-adr-competitive-thesis-swot.md) / [ADR 006](2026-08-23-adr-strategy-frameworks-beyond-swot.md)).

## Context

The dashboard is read-by-eye: feed, KPIs, entity panels, framework strips. The owner
wants to **ask questions in natural language** — "which fintechs are heating up in
acquiring?", "what did Itaú change this week?", "who's exposed to the DICT rule?" — and
get an answer synthesized from **the tool's own ingested data**, not the open web.

Two hard requirements shape it:

1. **Grounded, cited, no fabrication** — the product's core rule. The agent answers
   *only* from Onça's data (narratives, beliefs/frameworks, registry, regulatory,
   feed), cites its sources, and says "I don't have that" rather than inventing.
2. **Curated / filtered asks** (the owner's explicit requirement) — the input must be
   **scoped to the tool's domain** (competitor/market intelligence over the tracked
   Brazilian financial-services universe). Off-topic asks (general knowledge, coding,
   personal, "ignore your instructions…") are refused/redirected, not answered.

The dashboard has no server today, but backend features are already exposed as Lambda
Function URLs mounted as `/api/*` CloudFront behaviors behind the same edge basic-auth —
so an agent endpoint is a natural addition, not new architecture.

## Decision

Add a **chat/ask block** to the dashboard backed by a new **read-only** agent endpoint
`POST /api/ask/` (Lambda Function URL → CloudFront behavior, ordered before the `/api/*`
catch-all). The agent is grounded in Onça's data and **gated by a curation layer**.

### 1. Backend — `OncaAgent` Lambda (`/api/ask/`)

- Input: `{ "q": <question>, "scope": {entity?, lens?, date?} }` (the dashboard passes the
  user's current filter as optional scope — "ask about this view").
- **Retrieval (the grounding):**
  - **KB Retrieve** over narratives (`ONCA_KB_ID`) for semantically relevant cards.
  - Structured reads of **`feed.json`**, the **entities registry**, and the belief stores
    (`swot/curated.json`, `<fw>/proposals.json`, `reg_lifecycle/index.json`) for exact
    facts (counts, tiers, deadlines, framework bullets).
  - Exposed to the model as **tools** (Bedrock Converse tool-use) — `search_narratives`,
    `get_entity_beliefs`, `list_feed(filter)`, `get_regulatory` — so it fetches only what
    it needs and every answer traces to concrete records.
- **Generation:** Bedrock Converse (nova-lite, the synth grant) with a strict system
  prompt: answer **only** from tool results, **cite** narrative ids / source URLs,
  distinguish sourced fact from **labeled inference**, refuse when unsupported.
- **Read-only:** no writes, no pipeline triggers, no registry mutation — it analyses, it
  doesn't act. (A future write-capable Agent API, #20, is out of scope here.)
- Cost/abuse bounds: max tokens, per-request timeout, and a simple rate limit.

### 2. Curation layer — "filter any ask" (the requirement, made first-class)

Two cheap gates *before* the expensive grounded call, plus a grounded-only generation
contract:

1. **Scope classifier (input gate).** A fast pre-check (a cheap model call or a
   keyword/embedding domain-similarity test) decides if the question is **in-domain**
   (about tracked entities, industries, regulatory, frameworks, or the feed/data). Out of
   domain → a canned redirect ("Só respondo sobre inteligência competitiva do mercado
   financeiro monitorado — tente …"), no expensive call made.
2. **Bedrock Guardrails.** Attach a guardrail to the Converse call: **denied topics**
   (non-domain, legal/medical/financial *advice*, personal data requests), **PII filter**,
   and **prompt-injection** mitigation. Retrieved document text is passed as *data*, never
   as instructions — the model is told to ignore instructions embedded in content.
3. **Grounded-only generation.** The system contract: no claim without a tool result to
   cite; "não tenho esse dado" when retrieval is empty; impact/prediction phrased as
   labeled inference; **LGPD/defamation discipline** (public figures in public roles only,
   no un-vetted person-graph assertions — same as the operatives/relational rule).

This layered gate is what "curated to filter any asks the owner might ask" means: scope
in, ground the rest, cite everything, refuse the rest.

### 3. Dashboard surface

- A **collapsible "Perguntar" (Ask) panel** with a text input + answer area (fits the
  single-page model; no new route). The answer renders **clickable citations** that
  deep-link to the cited card/entity (reuse the existing entity-filter + card anchors).
- Pre-seed **example prompts** (in-domain) so the owner sees the intended envelope.
- Optionally stream tokens (Function URL supports response streaming) for responsiveness.

## Consequences

- New per-question LLM + retrieval cost — bounded by the scope gate (off-topic asks never
  reach generation), token/time caps, and rate limiting.
- Reuses existing infra (Function-URL/behavior pattern, KB, belief stores, edge auth) — no
  new architecture; the endpoint inherits the dashboard's basic-auth.
- Precision/trust risk is the whole game: the curation layer + grounded-only + citation
  contract keep it from becoming a confident open-web chatbot. If it can't ground an
  answer, it must decline — that discipline is non-negotiable.
- Closes the UI/read-only half of #21 and #20; a write-capable Agent API stays a separate,
  later decision (it changes the risk profile entirely).

## Status / next steps

Proposed. Implementation order when picked up: (1) `OncaAgent` Lambda with KB Retrieve +
2–3 structured tools + the grounded system contract; (2) the scope classifier + Bedrock
Guardrail; (3) `/api/ask/` Function URL + CloudFront behavior (before `/api/*`); (4) the
dashboard Ask panel with clickable citations + example prompts; (5) rate limit + token
caps. Related backlog: `docs/2026-08-16-roadmap.md`.
