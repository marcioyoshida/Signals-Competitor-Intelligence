# CONTEXT — session primer

Read this with `CLAUDE.md` (decisions + conventions) and
`docs/DATA_SOURCES.md` (data catalog) at the start of any session. This
file captures the *reasoning* behind the decisions and the current state
of play, so a fresh session (or a returning human) knows not just what
exists but why, and what to check before trusting it.

---

## What Onça is, in one paragraph

An agentic-AI competitive-intelligence product for **Brazilian financial
services** (banks, insurers, fintechs, asset managers), sold on **AWS
Marketplace**. It fuses three signal lenses — regulatory (BCB/CVM/SUSEP),
competitor behavior, and market data — into one threat-scored "warroom"
feed where every synthesized claim links to its source document. The
buyer is a strategist/CI lead at a regulated institution who cannot cite
an AI that doesn't show its sources. The correlation/synthesis across the
three lenses is the actual product; the ingestion below is the raw
material for it.

## Why the key decisions were made (rationale, not just the choice)

- **Narrow to Brazilian financial services, not general CI.** The moat is
  free government data (BCB/CVM) that every English-first CI tool
  (AlphaSense, Klue, Crayon, Contify) ignores. Going broad forfeits that
  moat and puts us head-to-head with funded incumbents.
- **AWS-native, not Databricks.** Data volumes are moderate
  (registry dumps, filings, scraped pages), not Spark-scale. Databricks
  adds a second bill (DBUs) and a second platform, and complicates the
  Marketplace metering story. AWS-native = one bill, one billing surface
  the buyer already trusts.
- **S3 Vectors, never OpenSearch Serverless.** OSS has a ~$345+/mo idle
  floor that alone would blow the ~$100/mo prototype ceiling. This is the
  single most common Bedrock cost trap.
- **CloudFormation first, Terraform later.** Marketplace Quick Launch only
  supports CFN. Regulated buyers often require Terraform for their own IaC
  governance — build that module only when a real buyer asks, not upfront.
- **SaaS tier first, container in-account deployment later, with a bridge.**
  SaaS onboards in days and creates internal champions; container
  deployment ("your data never leaves your AWS account") is what a bank's
  security review can actually approve. The bridge lets one funnel feed
  the other. Graduation rate (SaaS→container) is the metric that tells us
  the business works.
- **Dashboard + agent + alerts, not a chatbot.** A strategist doesn't know
  to ask about a filing they haven't seen. The product must be proactive
  (agent monitors, pushes briefings), with a dashboard as the audit/trust
  surface and chat only as drill-down. Opaque autonomous agents fail the
  regulated-industry transparency bar.

## Data-source reasoning (the non-obvious findings)

- **"CIP/Nuclea data" = the BCB SPI dataset.** CIP runs settlement
  plumbing but publishes no open API; its stats reach the public only via
  BCB. So we ingest BCB SPI, not CIP. Live SPI EntitySets are **system
  aggregates** (no ISPB) — not a per-competitor TPV feed.
- **Per-institution Pix on open data = DICT keys (`ChavesPix`), not TPV.**
  Live Pix_DadosAbertos has no public per-ISPB transaction-value series.
  `ChavesPix(Data=…)` is the working per-ISPB signal (key stock by ISPB).
- **Autorizações = institutions in operation, not pending processes.**
  BcBase `EntidadesSupervisionadas` FunctionImport was 500 live; primary
  path is `Instituicoes_em_funcionamento` EntitySets (~1.7k entities).
- **Serasa APIs are paid per-query risk/credit data**, not competitive
  signal. Tier-3 enrichment at most, not core. Skip for MVP.
- **Cielo has no API**, but files as a listed company → its TPV/take-rate
  data lives in CVM filings. Stone/PagSeguro/Nu are US-listed → their data
  is in SEC EDGAR, not CVM. Rede/GetNet are invisible (consolidated inside
  Itaú/Santander).
- **LinkedIn hiring data: never scrape.** Highest-litigation source
  (LinkedIn actively sues). Use company career pages / ATS boards
  (Gupy/Greenhouse/Lever) for hiring signal, or a licensed aggregator.
- **Legal tiering:** government sources = zero risk (build freely); public
  web = logged-out only, respect robots.txt; login-gated = buy, don't
  build; auth-bypass/CAPTCHA = never.

## What's built (Phase 1 data spine + Phase 1.5 Lambda)

Six ingesters, pure `fetch_*` functions, plus a two-mode diff engine
(`detect_new` + `detect_moves` / `ValueState` + DynamoDB ports).

| Module | Source (live) | Signal | Diff | Local | Lambda |
|---|---|---|---|---|---|
| `bcb_normativos.py` | BCB Buscador de Normas | new regulatory docs | detect_new | yes | yes |
| `bcb_ifdata.py` | IF.data OData | market share | ranking | CLI | yes |
| `bcb_pix.py` | **ChavesPix** DICT keys | key-stock moves by ISPB | detect_moves | yes | yes |
| `bcb_autorizacoes.py` | **Instituicoes_em_funcionamento** | new entrants | detect_new (seeded) | yes | yes |
| `bcb_juros.py` | **taxaJuros v2 daily** | rate moves by product | detect_moves | yes | yes |
| `cvm_ofertas.py` | **CVM oferta-distrib ZIP** | capital raise / launch | detect_new (seeded) | yes | yes |
| `cvm_inf_diario.py` | **Informe Diário + RCVM175 reg** | fund AUM moves | detect_moves | yes | yes |
| `cvm_fundos.py` | CVM cad_fi (legacy) | watchlisted fund launches | detect_new | yes | yes |
| `sec_filings.py` | SEC EDGAR | US-listed fintech filings | detect_new | yes | **not yet** |

`run.py` → local digest; `src/ingest/lambda_port.py` → daily EventBridge
Lambda → DynamoDB state + S3 digests + raw corpus for Bedrock KB.

### Status as of 2026-07-19 (my2027 / 668449743071 / us-east-1)

- **Phase 1.5 live:** Lambda digest includes normativos, CVM funds, IF.data,
  **autorizações**, **Pix DICT keys**, and **juros médios** (daily rates).
  Smoke-tested Pix/autorizações (1,751 institutions / 872 ISPBs); juros
  wired with live schema (~799 daily rows, default modality filter).
- **Schemas live-aligned** for Pix, autorizações, juros (see DATA_SOURCES.md).
  Earlier catalog guesses (`TransacoesPix`, plain BcBase EntitySet) were wrong.
- **Phase 2 Stage A:** raw corpus writer + Bedrock KB (S3 Vectors) still
  provisioned; embedding `StartIngestionJob` still blocked on **0 on-demand
  Bedrock embedding throughput quota** (account provisioning, not config).
- **Phase 2 Stage B:** not started (synthesis/correlation Lambda).
- **SEC** remains local-only until User-Agent is real and wired to Lambda.

## CRITICAL: verify-before-trust

Several ingesters still carry catalog-era assumptions. **Pix and
autorizações are live-verified** as of 2026-07-19. Before building on any
other ingester, run its `inspect` against the live API:

    python -m src.ingest.bcb_normativos
    python -m src.ingest.bcb_ifdata
    python -m src.ingest.bcb_pix inspect
    python -m src.ingest.bcb_pix inspect-spi
    python -m src.ingest.bcb_autorizacoes inspect
    python -m src.ingest.cvm_fundos
    python -m src.ingest.sec_filings inspect     # needs real User-Agent first

The runner / Lambda wraps each source in try/except so one bad schema
doesn't break the digest — but the fix is always to align the field
mapping, not to leave it failing silently.

`sec_filings.py` still has a `REPLACE_WITH_YOUR_EMAIL` placeholder in
HEADERS — SEC blocks requests without a real contact UA.

## What's next (in priority order)

1. **Stage A quota completion** — track pending Embed V4 requests; open
   Support case for Titan Embed V2 once Support plan is enabled
   (`docs/AWS_BEDROCK_QUOTA_TICKET.md`); prove ingestion + Retrieve.
2. **Stage B harden** — deploy synthesis Lambda; live dry-run on real
   digests; tune candidate fusion; enable LLM when Converse works.
3. **Next data sources:** optional deeper SEC text extraction; BCB SCR.
4. **Phase 3** — dashboard + alerts (consume `narratives/` in S3).
5. **Phase 4** — design partners, then Marketplace SaaS listing.

Deferred: CNPJ bulk registry (multi-GB), SUSEP + Diário Oficial scrapers.

## Open decision (unresolved)

First design-partner profile is undecided. If payments/acquiring →
SEC ingester is high-value now (it's built and gated on `sec_tickers`).
If bank/insurer → empty `sec_tickers` and it stays dormant. This choice
also sets the first sub-vertical focus (leaning payments/fintechs for
regulatory cadence: Pix, open finance).

## Guardrails for any session working here

- Prototype cost ceiling ~$100/mo. Check any managed service's idle floor
  before adding it.
- Never present estimated/proxy numbers (market share, scores) without an
  explicit "estimated" label separating them from sourced figures.
- Every ingested record keeps a source URL — the citation trail is the
  product, not a nice-to-have.
- Don't relitigate settled decisions (see CLAUDE.md "should NOT do")
  without a concrete new reason.
