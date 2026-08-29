# ADR 011 — Entity discovery, enrichment & profile composition (a separate curated pipeline)

- Status: **Partial — first vertical LIVE (2026-08-28).** Design 2026-08-25;
  FIAGRO structured sync + keyword harvest shipped 2026-08-28. Remaining: general
  unresolved-mention NER, FII sibling, DFP/ITR → KB, dashboard "Descoberta" tab.
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

**Shipped 2026-08-28 (first vertical — FIAGRO / agri-funds):**
- `src/ingest/cvm_fiagro.py` — CVM FIAGRO Informe Mensal fetcher (CNPJ, ISIN→ticker,
  admin/gestor, PL). Live: ~280 classes.
- `src/synth/entity_discovery.py` — `discover_fiagro()` (structured strong-id auto-create
  / enrich under `agri-funds`) + `harvest_keyword("FIAGRO", news_items)` (propose-only
  news path, frequency-gated). Promotion policy matches §4.
- Wired in `lambda_port` behind `ONCA_ENTITY_DISCOVERY` (default **off** until first
  live validation). `ONCA_FIAGRO_MIN_PL` (default R$50mi) + `ONCA_ENTITY_DISCOVERY_AUTOCREATE`.
- Tests: `tests/test_cvm_fiagro.py`, `tests/test_entity_discovery.py`.

This closes the owner example: "few players in registry for Fiagro, plenty in news"
— the CVM universe is now the source of truth for agri-funds, and news-keyword
harvest proposes the rest.

**Hardened + generalized to any industry (2026-08-29).** The first cut targeted
FIAGRO literally and was written against an *imagined* registry API (called
`resolve_by_name` / `name_owned_by_other`, which did not exist; used a dict-shaped
`put_entity` and a `payload=`-shaped `propose_review` — so the structured
auto-create path threw on every row and the branch failed 3 of its own tests).
Fixed and made cross-industry:
- **Registry:** added `resolve_by_name()` (alias-index + display-name match, returns
  a list so a unique hit is enrichable and an ambiguous one goes to review) and
  `name_owned_by_other()` (the hijack guard); `propose_review()` gained an optional
  `payload` for curator evidence. `entity_discovery` now enriches via the real
  single-writer helpers (`set_industries`, `assign_ticker`).
- **`discover_fiagro(industry=...)`** — the promotion engine (resolve→enrich→create→
  propose) is now industry-parametric; only the CVM-FIAGRO fetcher + `_profile_from_fiagro`
  stay source-specific. Each new **structured** industry = 1 fetcher + 1 profile mapper.
- **`harvest_keyword`** generalized to *any* industry: accent/plural-tolerant keyword
  matching (a singular "fundo imobiliário" catches plural "fundos imobiliários");
  broadened B3 ticker to `XXXX\d{1,2}` (equities **and** funds — ITUB4/BBDC3, not
  only XXXX11); single-token proper-name capture (Neon, Nubank, Itaú) with
  keyword-word / generic-corp-word / sentence-initial filters. Empirically: a
  banking sample that previously yielded **0** now surfaces `ITUB4`, `Itaú`, `Neon`.
- Tests: `+test_resolve_by_name_and_name_owned_by_other`,
  `+test_harvest_generalizes_equity_ticker_and_single_brand`,
  `+test_harvest_keyword_accent_plural_tolerant`.

Remaining implementation order: (1) general unresolved-mention harvest across all
news/DOU (not just FIAGRO keyword); (2) FII sibling of `cvm_fiagro` (see
`2026-08-20-fii-structured-source-plan.md`); (3) CNPJ/Receita profile composition for
news-only candidates; (4) ingestion follow-up probe ("added but not surfacing");
(5) CVM DFP/ITR → KB (#7); (6) discovery/curation dashboard tab.
Related: `docs/2026-08-16-roadmap.md`, [ADR 010](2026-08-25-adr-agent-chat-ui.md).
