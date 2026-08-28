# ADR 011 — Entity discovery, enrichment & profile composition (a separate curated pipeline)

- Status: **Partial — first vertical LIVE (2026-08-28).** Design 2026-08-25;
  FIAGRO structured sync + keyword harvest shipped 2026-08-28. Remaining: general
  unresolved-mention NER, FII sibling, DFP/ITR → KB, dashboard "Descoberta" tab.
- Delivers **issue #14 (New Pipeline for Entity Discovery)**, **#22 (B3 ticker → entity)**,
  and **#7 (balance-sheet ingestion → KB for inference)**.
- Builds on: the entities registry as source-of-truth
  ([ADR 002](2026-08-17-adr-entities-registry.md)), auto-create-from-entrant,
  news-terms derivation, and the step-5 review queue.

## Context

The owner example is concrete: **Fiagro / agri-funds** has only a handful of players in
the registry, yet news is full of FIAGRO coverage. The same pattern will repeat for FII,
new BCB-authorized fintechs, and any industry whose official universe is larger than the
curated watchlist. Without a discovery pipeline, synthesis and the agent stay blind to
"unknown unknowns".

## Decision

Ship a **curated discovery + enrichment pipeline** (not a silent auto-writer) that:

1. **Harvests unresolved mentions** from news / DOU / structured sources.
2. **Matches against the Registry** (CNPJ, ticker, alias).
3. **Enriches** from open regulators (CVM, BCB, Receita) when a strong identity exists.
4. **Proposes** to the review queue (or auto-creates only on strong structured identity).
5. **Follows up** after an add ("added but not surfacing").
6. Later feeds **DFP/ITR balance sheets → KB** for financial grounding.

Precision-first: strong CNPJ/BCB/CVM structured identity → auto-create; news-only → review
queue. Nothing pollutes the registry silently.

## Stages (design)

### 1. Unresolved-mention harvest
Scan recent free-text (news, DOU, fatos) for entity-like tokens / B3 tickers that
``resolve_entities`` does **not** map. Frequency-gate (≥ N docs) + finance-context gate
to keep cost/precision bounded. Store candidates with evidence snippets.

### 2. B3 ticker detect / assign (#22 — already shipped for existing entities)
When a signal carries an ISIN or a bare 4-letter+11 ticker, attach it to the matching
entity (enrich first). New tickers that do not resolve become discovery candidates.

### 3. CNPJ / Receita profile composition
For candidates with a CNPJ (from CVM filing, BCB autorização, or news extraction):
compose a profile (razão social, trade name, CNAE → industry, controllers via QSA
review-gated, admin/gestor for funds). For FIAGRO/FII the CVM Informe already supplies
the strong key + metadata.

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

- Registry coverage expands without silent pollution or redeploys.
- Fiagro (and later FII) news becomes fully entity-resolvable once the CVM universe is synced.
- Agent / synth financial reasoning gains DFP/ITR grounding (#7).
- Review queue load is bounded by frequency gates + max_new caps.

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

Remaining implementation order: (1) general unresolved-mention harvest across all
news/DOU (not just FIAGRO keyword); (2) FII sibling of `cvm_fiagro` (see
`2026-08-20-fii-structured-source-plan.md`); (3) CNPJ/Receita profile composition for
news-only candidates; (4) ingestion follow-up probe ("added but not surfacing");
(5) CVM DFP/ITR → KB (#7); (6) discovery/curation dashboard tab.
Related: `docs/2026-08-16-roadmap.md`, [ADR 010](2026-08-25-adr-agent-chat-ui.md).
