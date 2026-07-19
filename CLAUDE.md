# CLAUDE.md — Signals / Onça competitor intelligence

## What this project is

"Onça" — an agentic AI competitive intelligence platform for **Brazilian
financial services** (banks, insurers, fintechs, asset managers). It fuses
three signal lenses into one threat-scored warroom feed:

1. **Regulatory** — BCB normativos, CVM rules, SUSEP circulars
2. **Competitor** — CVM fund filings, licensing events, hiring, pricing
3. **Market** — IF.data market share, IBGE sector data, funding events

Differentiator: signal **fusion with source citations** (every synthesized
claim links to the original filing). Target buyers work in regulated
institutions and cannot cite an AI that doesn't show its source.

Distribution: **AWS Marketplace**. SaaS tier first (fast onboarding),
container in-account deployment later for regulated buyers, with a bridge
migration path between the two.

## AWS account

- Account alias: `my2027`
- Account ID: `668449743071`
- CLI profile name convention: `my2027`
- Region: `us-east-1` for the prototype (broadest Bedrock model
  availability, cheapest). Revisit `sa-east-1` (São Paulo) only when a
  customer's data-residency requirement demands it — Bedrock model
  coverage there is narrower; verify before committing.

## Architecture (decided — don't relitigate without reason)

- **AWS-native, serverless. No Databricks.** Rationale: moderate data
  volumes, single billing surface for Marketplace, no second platform.
- Ingestion: Lambda + Glue, scheduled by EventBridge, orchestrated by
  Step Functions
- Storage: S3 / S3 Tables (Iceberg) for structured; Lake Formation for
  row/column governance
- RAG: Bedrock Managed Knowledge Base backed by **S3 Vectors**
  (NOT OpenSearch Serverless — it has a ~$345+/mo idle floor)
- Reasoning: Bedrock AgentCore pattern (cheap models — Nova/Haiku — for
  routing and classification, stronger models only for synthesis; batch
  inference for non-real-time jobs; prompt caching on the shared corpus).
  Phase 2 Stage B implements this pattern via direct Bedrock Retrieve +
  Converse calls from a plain Lambda, not a container-hosted AgentCore
  Runtime — this is a once-daily batch job with no session state, so
  Runtime's container/artifact overhead buys nothing yet. Revisit true
  AgentCore Runtime if/when an interactive dashboard agent needs real
  session semantics (Phase 3+). See
  docs/2026-07-12-phase2-stage-a-knowledge-base.md.
- Delivery: warroom dashboard (threat-scored feed, entity timeline,
  source drill-down) + EventBridge→SNS alerts/digest
- IaC: **CDK synthesizing to CloudFormation** (Marketplace Quick Launch
  only supports CFN). A hand-maintained Terraform module comes later,
  only when a regulated enterprise buyer requires it.

## Data sources (MVP = free government tier only)

| Source | Access | Signal |
|---|---|---|
| BCB Buscador de Normas | REST API | New regulatory documents |
| BCB IF.data (Olinda OData) | API | Quarterly institution financials → market share |
| CVM Dados Abertos (cad_fi) | CSV | New fund filings by competitors |
| Receita Federal CNPJ | bulk CSV | Company universe (deferred — multi-GB) |
| SUSEP / Diário Oficial | scrape | Deferred — higher maintenance |

Rules: government sources first (zero legal risk); public web scraping
logged-out only, respect robots.txt; LinkedIn-derived data ONLY via a
licensed aggregator (People Data Labs / Explorium), never scraped.

## Phase plan and current status

- **Phase 0** — customer discovery (5–10 strategist interviews),
  Marketplace seller registration, AWS credits applications
- **Phase 1 (done)** — data spine: ingesters + diff engine + digest.
  Modules: normativos, IF.data, CVM funds, Pix DICT keys, autorizações,
  SEC (local), `diff/engine.py`, `run.py`. See DATA_SOURCES.md for
  **live-verified** Pix/autorizações schemas (2026-07-19).
- **Phase 1.5 (done, extended 2026-07-19)** — Lambda + EventBridge +
  DynamoDB state + S3 digests. Live digest sources: normativos, CVM
  funds, IF.data, **autorizações** (seeded detect_new), **Pix DICT keys**
  (detect_moves). Env from watchlist: competitors, ISPB list, Pix move
  threshold. SEC not on Lambda yet. Smoke-tested in my2027.
- **Phase 2 (CURRENT)** — Bedrock KB + synthesis loop with citations;
  correlation logic (regulatory event + competitor signal → one flagged
  narrative). This correlation IS the product.
  **Stage A (infra deployed, blocked on account quota)** — raw corpus to
  `onca-raw-{account}` + Bedrock KB (S3 Vectors). Corpus write live-ok;
  `StartIngestionJob` blocked by **0 embedding on-demand quota**. See
  docs/2026-07-12-phase2-stage-a-knowledge-base.md. **Stage B (not
  started)** — synthesis/correlation Lambda.
- **Phase 3** — dashboard + alerts
- **Phase 4** — design partners, then Marketplace SaaS listing

## Conventions

- Python 3.11+, type hints, small pure fetch functions (Lambda-portable)
- State behind a narrow interface (JsonState / ValueState local;
  DynamoDbState / DynamoDbValueState in Lambda)
- Every synthesized output must carry source URLs — no uncited claims
- Prototype cost ceiling: ~$100/month. Before adding any managed
  service, check its idle/floor cost.
- First sub-vertical focus: payments/fintechs (fastest regulatory
  cadence: Pix, open finance)

## Things Claude should NOT do in this repo

- Add OpenSearch Serverless (cost floor)
- Add scraping of login-gated sources (legal exposure)
- Present estimated/proxy numbers (market share, scores) without an
  explicit "estimated" label distinguishing them from sourced figures
- Invent API response schemas — verify against a live call first
