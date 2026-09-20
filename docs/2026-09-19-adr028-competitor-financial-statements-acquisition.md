# ADR 028 — Balance sheets and results: which acquisition route actually works

- Status: **Proposed** — 2026-09-19. Owner-requested: *"check how Balance Sheets and
  Results could be fetched by GDELT, Google Search or their site."*
- Complements **ADR 022** (financial-soundness / prudential ingestion — the *health*
  question) and **#7 / ADR 011 stage 6** (`cvm_financials.py` — the *statements*
  question). Constrained by **ADR 019** (a new source is a `SourceSpec` + one `FETCHERS`
  entry, not a bespoke Lambda) and the **`Signals-GDELT-Spine` S0 verdict** (2026-09-16).
- Does **not** propose a new pipeline. Everything below lands on the existing monthly
  `OncaFinancialsPipeline` or the existing ingest fan-out.

## Context

The question assumes balance sheets and results are a *gap to be filled by a new kind of
source*. Measured against the live system, that is not where the problem is. This ADR
records what was verified on 2026-09-19, assesses the three proposed routes honestly, and
routes the work to where the evidence points.

### What is live today (verified, not inferred)

| Store | Records | As of | What it carries |
| --- | ---: | --- | --- |
| `financials/index.json` (CVM DFP) | **13** | 2026-08-30 | assets/equity/revenue/net income, **all `period = 2024-12-31`** |
| `balancete/index.json` (COSIF 4010) | 69 | 2026-09-06 (`latest_month` 202606) | crédito, depósitos, PL, disponibilidades, PDD |
| `resultados/index.json` (COSIF 4010) | 69 | 2026-09-06 | opex ÷ ativo |
| `bcb_ifdata/index.json` | 419 | 2026-09-19 | quarterly summary aggregates → market share |

Registry: **11,289 rows**, ~1,794 active entities (the GDELT S0 census). So a genuine
balance sheet *and* income statement exists for **13 of ~1,794 tracked entities**, and
those 13 are a full fiscal year stale.

### Three defects in the path that already exists

1. **It is not running.** `ONCA_FINANCIALS` is **absent** from the deployed ingest
   Lambda's environment (`OncaPrototypeStack-OncaLambdaPrototype…`, checked live). The
   block at `src/ingest/lambda_port.py:1115` is gated `default false`, so
   `financials/index.json` is a frozen artifact of a manual run on 2026-08-30 — not a
   refreshing source.
2. **Annual only.** The call site hardcodes `doc="DFP"` (`lambda_port.py:1126`).
   `cvm_financials.fetch_statements` supports ITR, and `itr_cia_aberta_2026.zip` is live
   (HTTP 206 today) — quarterly *results* for listed competitors are simply never fetched.
3. **A year default that guesses.** The year default is `today.year - 1`.

   > **Correction (2026-09-19, while implementing #145).** This ADR first claimed the
   > default was "stale by construction" because DFP package year *N* holds fiscal year
   > *N−1*. That is wrong: the package year is the fiscal **reference** year.
   > `dfp_cia_aberta_2025.zip` carries `DT_REFER = 2025-12-31` for 438 issuers, and
   > `dfp_cia_aberta_2026.zip` carries 8 — only those whose fiscal year has already ended
   > in 2026. So `today.year - 1` is right for most of the year and collapses every
   > January–March, when last year's DFPs have not been filed yet. The store's FY2024
   > contents therefore came from a pinned `ONCA_FINANCIALS_YEAR`, not from the default.

   Either way the fix is the same and is what #145 asked for: pick the newest package that
   actually carries filings, by **content**, not by arithmetic on today's date. The floor
   has to be an issuer count rather than a truthiness check — an in-progress package is
   not empty, it is sparse, and a bare `if stmts:` would have taken the 8.

### The 4016 finding — #92's stated blocker is wrong

[#92 (SURF-12, cost-to-income / custo-de-crédito)](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/92)
is marked BLOCKED "needs DRE doc 4016 ingester". The monthly COSIF file Onça **already
downloads every month** was inspected (`202606BANCOS.CSV`, 87,555 rows). Its own header
reads `Balancete/Balanco Geral (Documentos: 4010 - 4016 - 4020 - 4026)`, and the
account-group distribution is:

| Doc | Institutions | Groups present |
| --- | ---: | --- |
| **4010** | 169 | 1, 2, 3, 4, **6, 7, 8**, 9 |
| 4016 | 169 | 1, 2, 3, 4, 6, 9 |

**Doc 4016 has *fewer* result accounts than 4010, not more.** Groups 7 (receitas) and 8
(despesas) — the result accounts — appear **only** under 4010, which
`bcb_balancete.py`/`bcb_resultados.py` already parse and then discard. Specifically, both
halves of the gross-up that #92 was blocked on are present in the file already on disk:

    8199200005  (-) DESPESAS DE PROVISÃO PARA RISCO DE CRÉDITO   149 institutions
    7199200006  REVERSÃO DE PROVISÃO PARA RISCO DE CRÉDITO       139 institutions

Net cost of credit is the difference. The honest caveat in `bcb_resultados.py` — that a
textbook cost-to-income needs a net operating-income denominator "the 4010 gross accounts
don't cleanly give" — was written without group 7 in view. Whether group 7 nets to a
usable denominator is now a **measurement**, not a blocker, and it costs zero additional
network.

Also visible: the file carries **169 institutions**; the store resolves **69**. Name
resolution, not acquisition, is dropping ~59% of the available balance sheets.

## The three routes assessed

### 1. GDELT — **Rejected.** Not a retry of a close call; a category error.

GKG is a document-metadata corpus: themes, tone, locations, names, and `V2.1Amounts`
(numerics scraped from prose with a character offset). It contains **no financial
statements at all** — there is no account taxonomy, no period, no consolidation basis, no
currency discipline. The best case is "R$ 5,2 bilhões" lifted from a sentence with no
reliable binding to an issuer, a line item, or a reporting period. Feeding that into a
balance-sheet store would manufacture exactly the unlabelled proxy CLAUDE.md forbids.

The entity-binding half is already measured and closed: `Signals-GDELT-Spine` S0 ran 30
days over 1,794 live Onça entities and got **7.4% (21.5% on the curated stratum)**, both
under the 25% kill line, root-caused to GKG's translate-then-NER pipeline destroying
Portuguese company names. That verdict stands; this ADR does not re-open it. GDELT's one
surviving Onça use is `gdelt_macro.py` — *themes*, deliberately never entities (#137).

**No spike is warranted.** Re-testing would measure the same thing twice.

### 2. Google Search — **Rejected as a source; redundant as a discovery mechanism.**

Three independent reasons, in descending order of force:

- **It returns links, not statements.** Even a perfect search integration ends at a URL.
  Something still has to fetch and parse the document — which is route 3. Search is at
  most a *discovery* step, never an acquisition one.
- **`google.com/robots.txt` says `Disallow: /search`** (fetched 2026-09-19). Scraping
  result pages is out. The sanctioned alternative, the Custom Search JSON API, is capped
  at 100 free queries/day then $5/1,000 with a 10k/day ceiling, and still only returns
  links.
- **Onça already has this capability.** `src/ingest/search_expansion.py` is a working
  web-search adapter (Tavily, `topic=finance`, `include_domains`, `language=pt`), built
  for entity discovery and fail-closed without a token. Any URL-discovery need is served
  by adding queries there. Adding Google as a second search dependency buys nothing the
  existing adapter does not already do, and costs a new credential and a new rate limit.

**Decision:** no Google integration. If IR-document discovery needs search, it extends
`search_expansion.py` with `include_domains` scoped to issuer IR hosts.

### 3. The issuers' own sites — **Accepted.** The only route of the three with real content, and better than expected.

Probed live 2026-09-19. Brazilian IR sites are not a long tail of bespoke scrapes: a large
share run on one vendor platform, **MZiQ (MZ Group)**.

- `ri.bancointer.com.br`, `investors.stone.co` and `ri.bb.com.br` all serve the same
  MZiQ-shaped `robots.txt` (`Allow: /`, WordPress admin paths disallowed — nothing
  relevant blocked) and all embed documents under one stable CDN pattern:

      https://api.mziq.com/mzfilemanager/v2/d/{company-uuid}/{document-uuid}?origin=2

  with a stable per-company UUID (Inter `b4dc0b14…`, Stone `46ed7b29…`, BB `5760dff3…`).
- One such URL on Inter's site was fetched: **HTTP 200, 1,243,382 bytes,
  `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`.** Its sheet names:

      2. BS | BP        ← Balanço Patrimonial
      3. IS | DRE       ← Demonstração do Resultado
      4. Funding   6. NII   8. Expenses
      9.1 Asset Quality   9.2 NIM & Yields   9.4 Efficiency
      9.8 ROE & ROA   9.9 Capital | Basileia

This is the literal answer to the question, in a machine-readable workbook: no OCR, no
PDF table extraction, no LLM parsing. It is **richer than CVM DFP** (NIM, efficiency
ratio, cost-to-serve, ARPAC, Basileia — metrics ADR 022 computes indirectly), **quarterly**,
and published on results day, months ahead of the CVM structured package.

Two constraints found, both manageable:

- The MZiQ directory endpoint `…/mzfilemanager/v2/d/{company-uuid}` returns **401**. There
  is no listing API; document UUIDs must be harvested from the IR page HTML. That makes
  the adapter *discovery-then-fetch*, and makes a results-day trigger valuable (below).
- Workbook layout is per-issuer. Sheet *names* are conventional (`BP`, `DRE`) but row
  labels are not standardised across issuers, so a mapping must be pinned per issuer and
  cited in-store — the same discipline `bcb_balancete.LINE_CODES` already applies to
  COSIF. Do not infer line meanings at runtime.

**The trigger.** `cvm_ipe.py` already ingests CVM IPE — every eventual disclosure a listed
company files, with `Categoria`/`Assunto`/`Data_Entrega`. Results releases pass through it.
That gives a free, authoritative "this issuer reported today" event to hang the IR fetch
on, instead of polling dozens of IR sites on a guess.

## Decision

1. **Fix and enable the CVM path first.** It is built, tested, and off. Three defects
   (not running / annual-only / stale-by-construction) stand between the product and a
   refreshing statements store. This is the highest value per unit of work in the whole
   question and involves no new source.
2. **Mine doc 4010's groups 7 and 8**, already downloaded monthly. Unblocks #92's real
   blocker and costs zero network.
3. **Build an MZiQ IR adapter**, spike first — measure how many tracked listed
   competitors are on the platform and how stable the workbooks are before committing to
   the parser. Trigger off `cvm_ipe`.
4. **Reject GDELT and Google Search** for this purpose, and record the reasoning here so
   the question is not re-litigated. Web search stays available for URL discovery only,
   through the existing Tavily adapter.

Ordering is deliberate: 1 and 2 are corrections to existing, paid-for infrastructure. 3 is
new capability and should not be started while the built path sits switched off.

## Consequences

- Statement coverage is currently overstated by the presence of a store that does not
  refresh. Until (1) ships, `financials/index.json` should be read as an FY2024 snapshot,
  and anything downstream that dates it implicitly is wrong.
- Route 3 introduces Onça's first *vendor-platform* dependency for financial data. MZiQ
  can change its HTML or its CDN pattern without notice. The adapter must fail closed (no
  invented rows) and be covered by `source_health.py`, like any other source.
- Accepting route 3 does not make the IR site authoritative. CVM/BCB filings remain the
  provenance tier of record; IR workbooks are `enrich`-tier under ADR 018 and must never
  overwrite a structured filing.

## Rejected alternatives

- **GDELT `V2.1Amounts` as a statements source** — numerics without issuer, line-item or
  period binding. Rejected on category, and separately on the measured 7.4% entity match.
- **Google Custom Search JSON API** — returns links, is capped and paid, and duplicates
  the Tavily adapter already in the repo.
- **Scraping google.com/search** — `Disallow: /search`. Not considered further.
- **A bespoke scraper per IR site** — rejected in favour of one vendor adapter plus an
  explicit per-issuer line map; the long tail can be added later, priced individually.
- **A new pipeline for any of this** — ADR 019 and ADR 022 both already route this class
  of work onto `OncaFinancialsPipeline`. No new state machine.

## Stories

| # | Story | Route | Effort |
| --- | --- | --- | --- |
| [#145](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/145) | F1 — CVM statements ingester is off, annual-only, a year stale — **SHIPPED 2026-09-19** | CVM (existing) | M |
| [#146](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/146) | F2 — mine COSIF 4010 groups 7/8; #92's stated blocker is wrong | COSIF (existing) | M |
| [#147](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/147) | F3 **spike** — MZiQ coverage + workbook stability, with a kill criterion | IR sites | S–M |
| [#148](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/148) | F4 — IR results-workbook adapter, triggered off CVM IPE (**gated on #147**) | IR sites | L |
| [#149](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/149) | F5 — COSIF name resolution drops ~59% of balance sheets already downloaded | COSIF (existing) | M |

No story is raised for GDELT or Google Search. That is the decision, not an omission.
