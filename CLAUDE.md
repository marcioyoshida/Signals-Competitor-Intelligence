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
| CVM Dados Abertos (registro_fundo_classe / RCVM 175) | ZIP CSV | New fund/class filings by competitors |
| Receita Federal CNPJ | bulk CSV | Company universe (deferred — multi-GB) |
| SUSEP / Diário Oficial | scrape | Deferred — higher maintenance |

Rules: government sources first (zero legal risk); public web scraping
logged-out only, respect robots.txt; LinkedIn-derived data ONLY via a
licensed aggregator (People Data Labs / Explorium), never scraped.

## Phase plan and current status

- **Phase 0** — customer discovery (5–10 strategist interviews),
  Marketplace seller registration, AWS credits applications
- **Phase 1 (done)** — data spine: ingesters + diff engine + digest.
  Modules: normativos, IF.data, CVM funds, CVM ofertas, Pix DICT keys,
  autorizações, juros médios, SEC (local), `diff/engine.py`, `run.py`.
  See DATA_SOURCES.md for **live-verified** schemas (2026-07-19).
- **Phase 1.5 (done, extended 2026-07-19)** — Lambda + EventBridge +
  DynamoDB state + S3 digests. Live digest sources: normativos, CVM
  funds, IF.data, **autorizações**, **Pix DICT keys**, **juros médios**,
  **CVM ofertas** (seeded detect_new). Env from watchlist includes
  ofertas lookback/watchlist, SEC EDGAR (seeded; STNE/PAGS/NU/INTR/XP).
- **Phase 2 (done 2026-08-14)** — Bedrock KB + synthesis loop with
  citations; correlation logic (regulatory event + competitor signal → one
  flagged narrative). This correlation IS the product.
  **Stage A (done)** — raw corpus to `onca-raw-{account}` + Bedrock KB
  (S3 Vectors, KB `CQ5LBZBQTY`). Titan V2 embed quota 60 RPM approved
  2026-08-10; ingestion + cited retrieval validated.
  **Stage B (live 2026-08-14)** — `src/synth/` synthesis Lambda producing
  LLM-written (nova-lite Converse) + KB-retrieved, source-cited fused
  narratives → `narratives/{date}/{id}.json`. Guardrails: citation scrub
  (`scrub_fake_url_tokens`) + absolute-PL AUM floor. `ONCA_SYNTH_USE_LLM/
  USE_KB=true`.
  **Orchestration (2026-08-14)** — `OncaPipeline` Step Functions state
  machine, `IngestTask → SynthTask`, one daily schedule (replaced the two
  standalone EventBridge rules). Ingest hardened after a 15-min-timeout
  root cause: `DynamoDbState` seen-set now sharded (`src/diff/engine.py`),
  per-source budgets + corpus cap (`src/ingest/lambda_port.py`). Ingest
  ~45–57s, pipeline ~90s green. See
  `docs/2026-08-14-phase2-pipeline-and-hardening.md`.
- **Phase 3 (CURRENT)** — warroom dashboard (done 2026-08-16) + alerts (next).
  Dashboard: static S3 + CloudFront (OAC) site with edge basic-auth
  (CloudFront Function), fed by `feed.json` aggregated by
  `src/dashboard/feed_builder.py` and wired as a 3rd pipeline step
  (ingest → synth → feed). Buildless single-file UI in
  `src/dashboard/site/index.html` (threat-scored feed, KPI tiles, entity
  timelines, source drill-down). Deployed + validated end-to-end; URL is
  the `DashboardUrl` stack output. Design:
  `docs/2026-08-14-phase3-dashboard-plan.md`. Threat scoring is a bounded,
  env-configurable blend (legacy saturated 1.0s recomputed on read 2026-08-22);
  remaining Phase 3: SNS/email alerts.
- **Pipeline & CI/CD (2026-08-25)** — the Step Functions pipeline is now
  **parallelized** (two-phase: BeliefAxes → Detectors incl. the SWOT+frameworks
  fan-out; ~340s→273s; ingest/news is the remaining bottleneck) and CI/CD moved
  to **AWS CodePipeline + CodeBuild** (`OncaCicdStack`, `buildspec.yml`), replacing
  GitHub Actions — pending a one-time GitHub CodeStar-connection authorization.
  Living backlog + shipped log: `docs/2026-08-16-roadmap.md`.
- **Intelligence layer (2026-08-25 afternoon → 2026-08-26, HEAD `bb58049`)** —
  agent + distress + coverage loop + framework evidence gate + topics.
  - **ADR-010** grounded Q&A LIVE (`OncaAgent` `/api/ask/` + dashboard Perguntar;
    #21 closed; write-capable Agent API #20 still open).
  - **ADR-012** entity-tagged RJ/falência store mined from news
    (`distress/index.json`); DataJud stays anonymized macro trend.
  - **ADR-013** distress A+B+C + queryable ownership/certifications on `ENT#`;
    **#33 CLOSED** — `attribution_role` gates observer entities (B3/Serasa/
    regulators); distress mining binds the RJ clause to its subject (not the
    co-mentioned actor); agent distress-intent questions ground **only** on
    `distress:` store cards (never a news card that names a third-party filing).
  - **ADR-014** coverage-gap loop + dashboard **Pontos Cegos** drawer + Remediar;
    AUTO_CODEGEN hard-off.
  - **#31 CLOSED** — BCB Ranking de Reclamações LIVE (`bcb_reclamacoes`);
    Reclame Aqui adapter parked (Cloudflare 403, no evasion).
  - **#32 CLOSED** — six non-TOWS frameworks own-track evidence (no SWOT quorum /
    no SWOT in the draft prompt) + per-dimension **axis-OR-lens** gate
    (`src/synth/framework_common.py`).
  - **#34 Phase 1+2** — unified `topic` derived at **feed-build** time
    (`src/dashboard/topics.py`), dashboard filter + agent ranking-only boost.
    Optional remaining: persist `topic` onto raw narratives.
- **Synthesis-layer evolution (on `main` 2026-08-23)** — narratives grew
  from cross-sectional to longitudinal/threaded/relational, and gained a
  per-entity competitive thesis. Pipeline order is now
  `ingest → synth → swot → swot_reconcile → swot_seed → threads → feed`.
  - **ADR-003 (fully built 2026-08-23)** — narrative dimensions: multi-axis
    synthesis across a `(subject_type, subject_key, axis)` design space.
    Shipped Wave 1 (comparative/peer-cohort, thematic/sector,
    regulatory-lifecycle/deadline, cohort/vintage), Wave 2 (incident
    threading + shared thread store, reg-lifecycle thread, behavioral/
    campaign), Wave 3 (relational graph + operatives/person layer,
    review-gated), and Opportunistic axes (predictive time-gated, ecosystem
    source-gated). See `docs/2026-08-19-adr-narrative-dimensions.md`.
  - **ADR-004 (built 2026-08-23)** — per-entity SWOT belief store that
    narratives reinforce/contradict. `src/synth/swot_store.py` (beliefs
    rebuilt each run from deterministic feeders + durable curated store),
    `swot_reconcile.py` (LLM stance + embeddings), `swot_seed.py`
    (cold-start LLM draft, evidence-cited, proposed-only), plus belief
    maintenance (drift/staleness re-review). Precision-first: no un-vetted
    bullet is ever `active`. See
    `docs/2026-08-22-adr-competitive-thesis-swot.md`.
  - **Analyst vetting UI (Phase C, 2026-08-23)** — war-room proposal panels
    (SWOT reconcile/seed + relationship graph) gained Aprovar/Rejeitar
    buttons POSTing to the CloudFront-fronted, origin-secret-gated vetting
    endpoint; decisions in `src/synth/curate.py` promote approved claims
    into durable stores so they survive each rebuild.
  - **Operatives / person-graph (2026-08-23)** — watchlist QSA ingestion +
    masked-CPF control cohorting; curated CNPJ-roots registry widened
    QSA/person-graph coverage 3→25. See
    `docs/2026-08-23-story-operatives-ingestion.md`.
  - Living backlog: `docs/2026-08-16-roadmap.md`. Source lenses added
    2026-08-16: CVM/B3 material facts (`fatos`), DOU (SUSEP/CADE/PREVIC),
    trade press/news RSS.
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
