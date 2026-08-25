# ADR 011 — Entity discovery, enrichment & profile composition (a separate curated pipeline)

- Status: **Proposed (design only)** — 2026-08-25. Owner-requested. No code ships here.
- Delivers **issue #14 (New Pipeline for Entity Discovery)**, **#22 (B3 ticker → entity)**,
  and **#7 (balance-sheet ingestion → KB for inference)**.
- Builds on: the entities registry as source-of-truth
  ([ADR 001](2026-08-17-adr-entities-registry.md)), its `auto_create_from_entrant` /
  `classify_industries` / step-5 **review queue**, `receita_cnpj` (BrasilAPI CNPJ →
  razão social / CNAE / QSA), the news-term-cap + "adding entities ≠ volume" findings,
  the Bedrock KB, and the parallelized pipeline ([issue #10]).

## Context

The registry is hand-curated + seeded, and we grow it in manual batches (mid-size banks,
2nd-tier fintechs, …). That leaves three gaps:

1. **Unknown unknowns.** Companies recur in the ingested texts (news/DOU/CVM/fatos) that
   are **not in the registry** — they surface as *unresolved mentions* and are invisible
   to synthesis. We only add what we happen to think of.
2. **No market identity.** Listed competitors have **B3 tickers** (ITUB4, BBDC4…) that
   appear in headlines but aren't linked to their entity — so ticker-keyed news/filings
   don't cluster, and the dashboard can't show a ticker.
3. **No financials.** We reason over news/regulatory/frameworks but not **balance sheets**
   — which are open data for listed issuers and are exactly what strategic frameworks
   (BCG share/growth, Porter) and the agent (ADR 010) want to ground on.

This is a **pure ingestion + curation gap** (like the operatives person-graph): the
resolvers and registry exist; what's missing is a producer that *finds*, *enriches*, and
*proposes* — under strict precision so the registry isn't polluted.

## Decision

A **separate "Entity Discovery & Enrichment" pipeline** (its own schedule/branch, decoupled
from the daily run — mention-harvest can run daily, CNPJ/DFP enrichment weekly). Six stages,
reusing existing machinery, **curated end-to-end** (proposals, not silent writes):

### 1. Harvest unresolved mentions
Scan the run's ingested texts (news, DOU, fatos, CVM) for candidate company names/brands
that **do not resolve** to any registry entity (`resolve_entities` returns nothing for the
span) yet recur with **finance context**. Gate on frequency (≥N distinct docs/publishers)
+ the finance-context filter — precision over recall, same discipline as the news
corroboration floor. Output: candidate surface forms + evidence (doc ids).

### 2. Detect & assign B3 tickers (#22)
Regex B3 ticker shapes (`^[A-Z]{4}\d{1,2}$`, e.g. ITUB4, BBDC4, KLBN11) in the texts;
map each to an issuer via **proximity to a resolved issuer name** and the **CVM issuer
registry** (companhias abertas — CNPJ↔ticker↔name). Then:
- existing entity → set its `ticker` field (enrichment);
- unknown issuer → becomes a discovery candidate (stage 1) carrying a strong structured id.

### 3. Enrich from structured sources (Receita / CNPJ / CVM)
For a candidate, resolve its **CNPJ** (CVM issuer registry for listed; BrasilAPI/Receita
by name otherwise), then pull `receita_cnpj` data to compose a **detailed profile**:
razão social + fantasia (→ `display_name`/aliases), **CNAE → industry** (`classify_industries`),
`cnpj_roots`, QSA **controllers**, capital. Listed → attach `ticker` + `fatos_term`
(structured identity, so it resolves from filings not fragile news).

### 4. Curated proposal (never silent pollution)
Emit an entity **create/enrich proposal** to the **step-5 review queue**, with the composed
profile + evidence. Promotion policy:
- **Strong structured identity** (CNPJ from a CVM/BCB filing, or a B3 ticker matched to the
  CVM registry) → eligible for **auto-add** at high confidence (mirrors the BCB-entrant
  `auto_create_from_entrant` precedent).
- **News-only brand** → **propose only** (analyst vets) — and if the bare name is a common
  word, carry `ambiguous_tokens` + a distinctive `news_term` (the 2nd-tier-fintech lesson).
- The registry's **"another entity owns this name → review"** guard stands; nothing merges
  into a curated entity automatically.

### 5. Ingestion follow-up (did the add actually get pulled?)
After an entity is added, **verify it's being ingested** — the "adding entities ≠ volume"
lesson: confirm it's in the news query set **and under `ONCA_NEWS_MAX_TERMS`** (past the
cap it's silently skipped), probe recent coverage, and flag **"added but not surfacing"**
for attention (thin-coverage or common-word cases like Cora/Dock). This closes the loop the
owner asked for — "follow up if the addition is being pulled or not."

### 6. Balance sheets → KB (#7)
Fetch **open-source financial statements** — CVM **DFP** (annual) / **ITR** (quarterly)
Demonstrações Financeiras for listed issuers (dados.cvm.gov.br; net-new fetcher, model on
`cvm_*`) for tracked entities. Store to the raw corpus and **ingest as KB documents** so
synth and the agent (ADR 010) can ground financial reasoning (BCG share/growth, margins,
leverage). Keyed by entity + period; bounded (tracked issuers only).

## Guardrails

- **Precision-first curation** — proposals, not silent writes; registry stays the
  source-of-truth; strong-structured auto-add vs news-only propose-only.
- **LGPD / person data** — CNPJ/company data is fine; **QSA person data stays review-gated**
  (operatives discipline), and **no full CPF stored/keyed** (existing constraint).
- **Cost/precision bounds** — frequency + finance-context gates on harvest; enrichment only
  for gated candidates; DFP/ITR only for tracked issuers; content-hash caching.

## Dashboard

Reuse the step-5 **review-queue UI** for a **"Descoberta / Entidades"** curation tab:
proposed entities with their composed profile (CNPJ, CNAE→industry, controllers, ticker) +
evidence, accept/reject; and an **"added but not surfacing"** watch list from stage 5.

## Consequences

- New enrichment cost (CNPJ lookups, DFP/ITR fetch+KB ingest) — bounded by gating + caching;
  runs on its own cadence, off the daily critical path.
- The registry grows by *discovery*, not just memory — and every listed entity gains a
  ticker + financials, unlocking better frameworks and grounded agent answers.
- Precision risk is the whole game: without the curation gate this pollutes the registry;
  with it, discovery is proposals a human (or the strong-id rule) promotes.

## Status / next steps

Proposed. Implementation order: (1) unresolved-mention harvest + candidate store; (2) B3
ticker detect/assign (enrich existing first — cheapest win); (3) CNPJ/Receita profile
composition → review-queue proposals; (4) strong-id auto-add + ingestion follow-up probe;
(5) CVM DFP/ITR fetcher → raw corpus → KB; (6) the discovery/curation dashboard tab.
Related: `docs/2026-08-16-roadmap.md`, [ADR 010](2026-08-25-adr-agent-chat-ui.md) (financials
feed the agent).
