# Data sources reference — BCB & CVM

Development reference for Onça ingesters. Every source here is free,
government-published, and legally clean (Tier 1 per CLAUDE.md). Ratings
reflect competitive-intelligence value for the payments/fintech and
financial-services buyer, not raw data richness.

**Access patterns:**
- **OData API** (`olinda.bcb.gov.br` / some CVM) — REST, JSON/CSV, paginated
- **Bulk CSV** (`dados.cvm.gov.br/dados/...`) — often ZIP-compressed, latin-1, `;`-delimited
- Prefer API over bulk where both exist; stream + filter bulk files, never load fully in memory

**Cadence caveat (BCB):** since 2025-03-26, daily historical-series
queries returning JSON/CSV are volume-limited. Pull monthly aggregates
and paginate rather than requesting long daily series in one call.

**Live schema note (2026-07-19):** Pix DICT keys and institution registries
below were verified against live Olinda responses from account/Lambda
smoke tests. Prefer those sections over older catalog guesses.

---

## Tier A — highest CI value (competitor behavior signals)

These reveal what a competitor is *actually doing*, not just what rules changed.

### BCB — Pix DICT keys (per-institution) *(in Lambda + local)*
- **What:** Monthly DICT Pix-key counts by institution (ISPB), broken down by
  key type × user nature (PF/PJ)
- **CI value:** ★★★★★ — closest **free per-ISPB** proxy to Pix footprint /
  traction. Monthly, fresher than IF.data quarterly. **Not** settlement TPV
  (BCB does not publish per-ISPB transaction value on this open service)
- **Access:** OData FunctionImport (requires `Data` date parameter)
  - Service: `olinda.bcb.gov.br/olinda/servico/Pix_DadosAbertos/versao/v1`
  - Call: `ChavesPix(Data=@d)?@d='YYYY-MM-01'&$top=10000&$format=json`
- **Live schema (ChavesPix row):**

  | Field | Type | Notes |
  |---|---|---|
  | `Data` | date | End-of-month as-of in response |
  | `ISPB` | string | Institution id (8 digits) |
  | `Nome` | string | Institution name |
  | `NaturezaUsuario` | string | PF / PJ |
  | `TipoChave` | string | e.g. Celular, CPF, CNPJ, Email, EVP |
  | `qtdChaves` | int | Key count for that slice |
  | `Segmento` | string | e.g. Instituição de Pagamento |

- **Normalized signal fields** (`src/ingest/bcb_pix.py`): `ispb`,
  `institution`, `segment`, `tx_count`/`tx_value` (= sum of `qtdChaves`),
  `anomes`
- **Diff:** `detect_moves` on `tx_value` by `ispb` (DynamoDB value state in Lambda)
- **Live volume (2026-06):** ~6.1k raw rows → **872** institutions after
  aggregation (top: NU PAGAMENTOS ~18.9% of keys)
- **Catalog:** dadosabertos.bcb.gov.br/dataset/pix
- **Implemented:** `src/ingest/bcb_pix.py` (`fetch_recent`, `by_institution`).
  Verify: `python -m src.ingest.bcb_pix inspect`

### BCB — SPI statistics (aggregate settlement; not per-ISPB)
- **What:** System-level Pix settlement series (quantity, total, primary/secondary channels) and PI-account remuneration
- **CI value:** ★★★☆☆ for market context — **no per-institution ISPB** on the open SPI service EntitySets verified live
- **Access:** OData — `olinda.bcb.gov.br/olinda/servico/SPI/versao/v1`
- **Live EntitySets (sample):** `PixLiquidadosAtual`, `PixRemuneracaoContaPI`,
  `PixLiquidadosIntradia`, `PixDisponibilidadeSPI`, `PixInterrupcaoSPI`
- **Live schema (`PixLiquidadosAtual`):** `Data`, `Quantidade`,
  `CanalPrimario`, `CanalSecundario`, `Total`, `Media`
- **Note:** CIP/Nuclea runs settlement plumbing but does not publish an open
  API; public SPI stats are via BCB. Do not expect per-competitor TPV here.
- **Implemented (CLI/inspect only):** `src/ingest/bcb_pix.py` (`fetch_spi`).
  Not wired into Lambda digest (no ISPB).

### BCB — Autorizações / institutions in operation *(in Lambda + local)*
- **What:** Registry of institutions currently in operation — proxy for
  “cleared authorization / active supervised entity.” BCB does **not**
  expose a clean pending-process feed
- **CI value:** ★★★★★ — **new-entrant early warning.** New CNPJ in the
  registry = competitor appearing
- **Access (live-verified primary path):**
  `Instituicoes_em_funcionamento` v1 EntitySets (no FunctionImport param):
  - `SedesBancoComMultCE` — banks + foreign branches
  - `SedesSociedades` — non-bank societies (incl. payment institutions)
  - `SedesCooperativas`
  - `SedesConsorcios`
  - URL pattern: `.../Instituicoes_em_funcionamento/versao/v1/odata/{EntitySet}?$top=10000&$format=json`
- **Live schema (common fields):**

  | Field | Notes |
  |---|---|
  | `CNPJ` | 8-digit root used as entity id |
  | `NOME_INSTITUICAO` | Legal name |
  | `SEGMENTO` | Present on banks/societies (entity type) |
  | `CLASSE` | Cooperatives (used when SEGMENTO absent) |
  | `UF`, `MUNICIPIO`, address/contact fields | Location enrichment |

- **Live volume:** ~**1,751** unique institutions across the four EntitySets
- **BcBase v2 fallback (currently broken live):**
  `EntidadesSupervisionadas` is a **FunctionImport(`dataBase`)** — not a
  plain EntitySet. Calls return HTTP 500 for known date forms as of
  2026-07-19. Richer cadastral fields exist in metadata
  (`codigoCNPJ14`, `nomeEntidadeInteresse`,
  `descricaoTipoEntidadeSupervisionada`, …) — revisit if BCB fixes the
  FunctionImport
- **Diff:** `detect_new` on `id=bcb-auth:{CNPJ}`; **first run seeds
  silently** (Lambda + local) so the full registry is not alerted as “new”
- **Implemented:** `src/ingest/bcb_autorizacoes.py`. Verify:
  `python -m src.ingest.bcb_autorizacoes inspect`

### BCB — SCR (aggregated credit operations)
- **What:** Monthly aggregated credit operations from the SCR credit information system
- **CI value:** ★★★★☆ — competitive lending picture: who's growing their loan book, which segments, how fast
- **Access:** monthly CSV, dadosabertos.bcb.gov.br
- **Signal:** competitor credit-portfolio growth rate + segment mix
- **Status:** not implemented

### BCB — Juros médios (average interest rates by institution)
- **What:** Average interest rates charged per institution (per Instrução Normativa nº 563, 2024-12-12)
- **CI value:** ★★★★☆ — competitor-level **pricing intelligence**, regulator-published. Feeds the radar "pricing aggressiveness" axis directly
- **Access:** dadosabertos.bcb.gov.br (structured)
- **Signal:** competitor rate changes over time by product
- **Status:** not implemented — next greenfield build priority

### CVM — Ofertas de Distribuição (securities offerings)
- **What:** Distribution offerings (shares, funds, debentures, CRI, etc.) registered or exempt (ICVM 400 / RCVM 160). Includes restricted-effort offers (ICVM 476) since 2022-01-01
- **CI value:** ★★★★☆ — competitor **capital-raising and product-launch** signal
- **Access:** bulk CSV, dados.cvm.gov.br
- **Columns of note:** Modalidade_Oferta, Data_Inicio_Oferta, Tipo_Societario_Emissor, Tipo_Fundo_Investimento
- **Signal:** competitor raising capital or launching a securitized product
- **Status:** not implemented

### CVM — cad_fi (fund registry) *(in Lambda + local)*
- **What:** Registry of structured + non-structured funds (ICVM 555 / Res. CVM 175), refreshed to last business day
- **CI value:** ★★★★☆ — new fund by a watchlisted admin = competitor product launch
- **Access:** `dados.cvm.gov.br/dados/FI/CAD/DADOS/cad_fi.csv` (latin-1, `;`)
- **Note:** funds adapted to Res. CVM 175 no longer listed in the legacy structured-funds cadastral file; TAXA_ADM / INF_TAXA_ADM columns now included
- **Implemented:** `src/ingest/cvm_fundos.py`

### SEC EDGAR — US-listed Brazilian fintechs *(local only; not yet on Lambda)*
- **What:** Filings for Brazilian fintechs listed in the US — Stone (STNE),
  PagSeguro (PAGS), Nu Holdings (NU), Inter&Co (INTR), XP (XP). Foreign
  private issuers file annual 20-F + interim/material 6-K; richest source
  of revenue, TPV, take rate, active-client counts for these players
- **CI value:** ★★★★☆ — the only clean disclosure path for US-listed
  acquirers/fintechs (their CVM footprint is thin or absent). Complements
  Cielo, whose equivalent data is CVM-only. Rede/GetNet stay invisible
  (consolidated inside Itaú/Santander)
- **Access:** free EDGAR APIs — company_tickers.json (ticker→CIK) +
  data.sec.gov/submissions. Requires a descriptive User-Agent w/ contact;
  <=10 req/s
- **Relevance gate:** only worth running if payments/acquiring competitors
  matter to the target customer — controlled by `sec_tickers` in
  config/watchlist.yaml (empty = skip)
- **Implemented:** `src/ingest/sec_filings.py`. Set a real User-Agent in
  HEADERS first; verify: `python -m src.ingest.sec_filings inspect`

---

## Tier B — market share & sizing

### BCB — IF.data (institution financials) *(in Lambda + local)*
- **What:** Quarterly institution-level assets, credit portfolio, deposits, equity
- **CI value:** ★★★★☆ — the sourced **market-share** axis. Quarterly, ~60 days lag (90 for Q4)
- **Access:** OData — `olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1`
- **Implemented:** `src/ingest/bcb_ifdata.py`

### CVM — Informe Diário de Fundos (daily fund reports)
- **What:** Per-fund daily: total portfolio value, net worth (PL), quota value, inflows/outflows (captações/resgates)
- **CI value:** ★★★★☆ — competitor fund **AUM and flows** = product traction + investor sentiment. TP_FUNDO column added
- **Access:** monthly ZIP CSVs, dados.cvm.gov.br/dataset/fi-doc-inf_diario (pre-2021 in /HIST)
- **Signal:** competitor fund AUM growth, net inflows vs. outflows
- **Note:** large — one file per month, ZIP-compressed; stream + filter to watchlist CNPJs
- **Status:** not implemented

### CVM — Companhias Abertas: DFP / ITR (financial statements)
- **What:** Standardized annual (DFP) + quarterly (ITR) financial statements of listed companies; now include "Dados da Empresa / Composição do Capital" and Pareceres e Declarações sections
- **CI value:** ★★★☆☆ — deep financials for *listed* competitors only (misses private fintechs)
- **Access:** bulk CSV per year, dados.cvm.gov.br/group/companhias
- **Signal:** listed-competitor revenue/margin/capital structure

---

## Tier C — market context (not per-competitor)

| Source | What | CI value | Access |
|---|---|---|---|
| BCB — Expectativas de mercado (Focus) | Daily market rate/inflation expectations | ★★★☆☆ macro backdrop | OData |
| BCB — Meios de pagamento / active cards | Active credit cards (12-mo activity), monthly +17d | ★★★☆☆ segment sizing | OData |
| BCB — Inadimplência (default rate) | % of SFN credit >90 days overdue | ★★★☆☆ risk environment | OData |
| CVM — Recompra de ações (buybacks) | Share buyback programs, daily update | ★★☆☆☆ listed-competitor signal | CSV daily |
| CVM — Cias Abertas cadastral | CNPJ, registration date/status of listed cos | ★★☆☆☆ entity resolution | CSV |
| CVM — Fundos Imobiliários / FIDC / FIAGRO informes | Real-estate / receivables / agri fund reports | ★★★☆☆ if vertical-relevant | monthly CSV |

---

## Tier D — infrastructure (low CI value; ignore unless a specific question needs it)

BCB Selic operations, STR/SPI settlement flows, títulos públicos auction
results, cédulas/moedas em circulação. Market-plumbing data, not
competitive signal.

---

## Intermediary/participant registries (entity resolution support)

CVM publishes cadastral data (refreshed last business day) for regulated
participants — useful for resolving who a competitor *is* and their role:
- Participantes Intermediários (brokers, banks, distributors)
- Coordenadores de Ofertas, Auditores Independentes, Agentes Fiduciários
- Consultores de Valores Mobiliários (incl. PLDFT/Compliance officers named)
- Plataformas de Crowdfunding: Informações Cadastrais

Use these to map a competitor's advisors, auditors, and offering
coordinators — relationship-graph enrichment, not a standalone signal.

---

## Pipeline vs Lambda coverage (2026-07-19)

| Source | Module | Local `run.py` | Lambda digest | Diff |
|---|---|---|---|---|
| BCB normativos | `bcb_normativos.py` | yes | yes | detect_new |
| CVM cad_fi | `cvm_fundos.py` | yes | yes | detect_new |
| BCB IF.data | `bcb_ifdata.py` | (CLI) | yes (snapshot) | ranking only |
| BCB Pix DICT keys | `bcb_pix.py` | yes | yes | detect_moves |
| BCB institutions in operation | `bcb_autorizacoes.py` | yes | yes | detect_new (seeded) |
| SEC EDGAR | `sec_filings.py` | yes | **no** | detect_new |
| BCB SPI aggregate | `bcb_pix.fetch_spi` | CLI | no | n/a |

Lambda env (from `config/watchlist.yaml` via CDK):
`ONCA_LOOKBACK_DAYS`, `ONCA_COMPETITORS`, `ONCA_COMPETITOR_ISPB`,
`ONCA_PIX_MOVE_THRESHOLD_PCT` (default 15), plus state/digest/raw/KB ids.

---

## Build priority (recommended order)

1. ~~**Pix statistics**~~ — DONE + **live-aligned** (`ChavesPix` DICT keys).
2. ~~**Autorizações**~~ — DONE + **live-aligned**
   (`Instituicoes_em_funcionamento`; first-run seed).
3. **Juros médios** — competitor pricing, feeds a dashboard axis.
4. **CVM Ofertas de Distribuição** — capital-raising / product-launch signal.
5. **CVM Informe Diário** — fund AUM + flows (heavier; ZIP streaming).
6. **SEC on Lambda** — after real User-Agent; optional for bank/insurer buyers.

Already implemented: IF.data (market share), cad_fi (fund launches),
BCB normativos (rule changes).

## Implementation notes

- Prefer live `inspect` over catalog docs — several catalog resource names
  (`TransacoesPix`, plain `EntidadesSupervisionadas`) do not work as
  plain EntitySets.
- Olinda **FunctionImports** need typed parameters (e.g. `ChavesPix(Data=)`);
  calling them as EntitySets returns `400 The URI is malformed`.
- CVM bulk files: latin-1 encoding, `;` delimiter, many ZIP-compressed.
- Respect the BCB daily-series volume limit — prefer monthly aggregates.
- Every ingested record keeps a source URL for the citation trail where the
  upstream provides one.
- Surface data-as-of dates in the product rather than implying freshness a
  quarterly/monthly feed lacks.
