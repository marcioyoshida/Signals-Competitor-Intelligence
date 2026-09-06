# ADR 022 — Financial-Soundness (Prudential) Ingestion + Financial-Tone NLP for BCB/CVM Institutions

Status: **PROPOSED** 2026-09-06. **Rev. 2 (2026-09-06)** — FinBERT reinstated as a
scale-to-zero *feature-extraction* stage (Batch Transform, FinBERT-PT-BR) after review;
the Rev. 1 blanket rejection conflated SageMaker with an always-on endpoint. See
*Revision note* and §3. **Pilot-validated (2026-09-06)** — the flow runs end-to-end on real
IF.data with FinBERT-PT-BR local on Python 3.14/CPU; see
[pilot findings](2026-09-06-adr022-pilot-findings.md).

Relates to / constrained by: ADR 019 (declarative source registry — a new source is a
`SourceSpec` + one `FETCHERS` entry, not a bespoke Lambda), the parallelized pipeline
(#10, `sfn.Parallel` ingest → feature → synth → …), ADR 003/006 (axes & strategy
frameworks over lenses — soundness is a new **belief axis**, not a new pipeline), ADR 004
(Titan-embed retrieval substrate + managed Bedrock KB `CQ5LBZBQTY` — the *existing* answer
to "no active vector DB"), ADR 013 (`entity_attrs.*` classification store), #7 (CVM DFP/ITR
`financials/index.json` + BCG), and the [[onca-reg-change-and-coverage]] / product-officer
ingestion arc (#74/#75) that this extends. **Supersedes the "ADR 01" single-Lambda sketch**
(2026-09-05) — see *Rejected alternatives*.

## Context

A sketch ("ADR 01") proposed monitoring banks/fintechs/corretoras by folding ingestion,
embedding and inference of COSIF/IF.data + risk-report PDFs into **one AWS Lambda** that
also writes its own **Parquet vector store to S3** and calls a **FinBERT endpoint** for
credit-risk sentiment.

Scrutinised against what is already live, the sketch is one genuinely valuable coverage
idea wrapped in three architectural moves that each *regress* an accepted decision:

**The real gap it identifies — prudential soundness.** Onça measures competitors' *size and
growth* but not their *financial health*:

- [`src/ingest/cvm_financials.py`](../src/ingest/cvm_financials.py) covers only the ~13
  **listed** issuers' DFP/ITR (revenue/assets/equity/net income → margin, YoY, leverage,
  BCG position, #7).
- [`src/ingest/bcb_ifdata.py`](../src/ingest/bcb_ifdata.py) pulls IF.data **Relatório T**
  (assets/credit/deposits/equity) → market *share* only.

Nobody computes **solvency and asset-quality soundness** — Índice de Basileia (capital
adequacy), inadimplência / NPL ratio, ROE/ROA, liquidez — and the ~thousands of **non-listed**
regulated institutions (fintechs, SCDs, corretoras, cooperativas) have *no* financial read at
all. For a competitor-intelligence product in a prudentially-regulated market, "is this
competitor sound, deteriorating, or over-exposed to credit?" is a first-order question we
cannot currently answer. That is the gap worth closing.

**What the sketch got wrong — and what it got right.** Two of its three architectural moves
conflict with accepted ADRs; the third (FinBERT) is a **good idea placed in the wrong stage**:

1. **One Lambda does ingest + embed + infer in a single execution.** The pipeline is a
   *deliberately parallelized* Step Functions DAG (#10): `sfn.Parallel` ingest fan-out →
   feature → synth → silence/longitudinal/comparative/thematic → feed, tasks coordinating
   only via S3. Re-monolithising re-serialises the ingest bottleneck and would hit the
   15-minute ceiling **exactly** on the large-PDF case the sketch itself flags as its risk.
   **Rejected.**
2. **Write vectors to S3 as Parquet, "eliminating active databases."** Onça *already* has no
   active vector DB — retrieval is a **managed Bedrock Knowledge Base** (S3 Vectors, KB
   `CQ5LBZBQTY`) for RAG plus an in-process cosine over **Titan Embed v2**
   ([`src/synth/embeddings.py`](../src/synth/embeddings.py)) for bounded SWOT reconcile.
   Hand-rolled Parquet vectors have **no ANN index** (a full scan per query) and duplicate a
   solved problem. **Rejected.**
3. **A FinBERT model for financial sentiment.** **Adopted, reframed** (Rev. 2). The sketch was
   right that a dedicated financial-sentiment model adds something Bedrock synthesis does not: a
   *deterministic, calibrated, cross-entity, over-time* numeric tone score over **Resultados e
   Balanços** text — a **feature**, not a narrative. What it got wrong was the *placement*
   (inside the monolith, as "inference") and the *implied* real-time endpoint. Run instead as a
   **scale-to-zero SageMaker Batch Transform** with **FinBERT-PT-BR**, the two Rev. 1 objections
   dissolve: Batch Transform has **no idle cost** (spins up, scores the daily corpus, tears
   down), and FinBERT-PT-BR is Brazilian-Portuguese, not English. See §3.

## Decision

**Adopt the domain content; reject the architecture.** Add financial-soundness as new
*data + axis + surfacing*, carried entirely by the existing pipeline, KB and stores.

### 1. A prudential-soundness ingester — `src/ingest/bcb_soundness.py`

Not a new Lambda. A new source under the ADR-019 registry. **Two complementary tiers, at their
own native cadences** — a quarterly *level* and a monthly *trajectory*:

- **Tier A — quarterly prudential ratios (the level).** The **same Olinda OData service**
  `bcb_ifdata` already calls — the prudential relatórios are just different `Relatorio` codes on
  that client. An *extension of an existing integration*, not a new one. **Pilot-confirmed source
  map** (see [pilot findings](2026-09-06-adr022-pilot-findings.md)): **`Relatório 5 "Informações de
  Capital"` under `TipoInstituicao=1`** carries `basileia_pct`, `capital_nivel_i_pct` (Tier 1),
  `capital_principal_pct` (CET1), `razao_alavancagem`, `indice_imobilizacao`, and the RWA breakdown
  — the full **solvency** set (values are fractions → ×100). `Relatório 1 "Resumo"` also exposes the
  headline Índice de Basileia for a lighter fetch. A coarse `soundness_band` (sólido / atenção /
  frágil) from the regulatory floor (8% + 2.5% conservation buffer ≈ 10.5% practical min). Quarterly
  `as-of`. **NB** the prudential-conglomerate `CodInst` under `TipoInstituicao=1` (capital) DIFFER
  from the `TipoInstituicao=2` codes the market-share flow uses — resolve against the
  `'<NAME> - PRUDENCIAL'` cadastro entries.
- **Liquidity is NOT in IF.data** (pilot-verified: no relatório carries an LCR/NSFR/liquidez
  column). True liquidity (LCR/NSFR) is published separately and lives in the banks' **Pilar 3**
  reports → it comes from **Phase 6** (Pilar 3 PDFs via the existing synth + KB path) or a dedicated
  LCR source, not the OData flow. A balance-sheet liquidity **proxy** is derivable but must be
  labelled inference, never presented as the reported LCR.
- **`inadimplencia_pct` (NPL) / `roe_pct`** come from the credit-portfolio + DRE relatórios
  (Relatórios 4, 7–14) or are derived from two reported lines — labelled inference when derived.

- **Tier B — monthly COSIF balancetes (the trajectory).** BCB publishes **monthly balancetes** per
  institution (COSIF *Balancete Patrimonial Analítico*, documento 4010 — a public "Balancetes e
  Balanços Patrimoniais" dataset). This is the **granular monthly series** the request asks for:
  raw account balances every month → **month-over-month deltas** on the lines that move first —
  carteira de crédito, **provisão/PDD** (loan-loss provisions), depósitos, patrimônio líquido,
  liquidez — giving a **12-point-a-year trajectory** between the quarterly Basileia snapshots.
  A rising PDD or deposit outflow shows up **months before** it lands in a quarterly ratio, so
  Tier B is the **leading indicator** and Tier A the confirmation. Monthly `as-of`. (Balancetes
  are large numeric files — the heavy structured parse — which is another reason they belong in
  the separate monthly pipeline of §4, not the 3×/day cycle.)

- **Honesty on ratios.** A ratio computed from two balancete lines is labelled **inference** with
  its formula + basis; a value BCB publishes pre-computed is labelled reported. Every field carries
  its `_prov` (ADR 018) and its `as-of` month/quarter; unresolved institutions stay **null**, never
  a fabricated number (CLAUDE.md no-unlabeled-proxy rule, same discipline as `bcb_ifdata`).
- **Resolution + store.** Reuse `bcb_reclamacoes.map_to_entities` name→`entity_id` resolution.
  Persist a durable `soundness/index.json` that holds **both** the latest level (Tier A) **and a
  monthly `series[]` trajectory (Tier B)** per entity; the monthly series also folds into
  `feature_store` / the `longitudinal` stage as per-entity rolling features. Merged into the #7
  `financials/index.json` read so a competitor card shows *size, growth, soundness AND its monthly
  trajectory* in one place. Covers the **non-listed** universe the CVM path cannot reach.

### 2. A financial-soundness **belief axis** (ADR 003), not a new inference stage

Soundness is surfaced through the mechanisms that already exist:

- A new axis feeds SWOT/frameworks: a deteriorating Basileia or rising inadimplência at a
  competitor is a **Strength** for us / **Threat** to them (mirrors the crédito-&-inadimplência
  thematic current already in [`src/synth/thematic.py`](../src/synth/thematic.py)). The **monthly
  Tier-B trajectory** sharpens this from a static level into a *direction* — the axis fires on a
  worsening **slope** (three months of rising PDD, steady deposit outflow) as an early warning,
  not only on a breached quarterly threshold.
- Surfaced on the **CRO** board (prudential risk of a competitor) and the **CPO** portfolio
  (soundness as a coverage/maturity signal), reusing the ADR-021 officer dashboards — no new
  screen.

### 3. A **financial-tone feature** over Resultados e Balanços — FinBERT-PT-BR on SageMaker Batch Transform

This is the distinctive, cross-entity enrichment the request asks for. It is a **feature stage**,
not a narrative stage — the two are complementary and both are kept:

- **Model.** **FinBERT-PT-BR** (a Brazilian-Portuguese financial BERT), classifying financial
  text into positivo / neutro / negativo with probabilities. A dedicated classifier gives what a
  general LLM does not: a **stable, calibrated score, identical run-to-run and comparable across
  every entity and every quarter** — a fixed measurement instrument, which is exactly what a
  belief axis needs. It is cheap enough to run over the **entire** entity universe, not just the
  ~13 listed issuers a per-entity Bedrock call could afford.
- **Compute — scale-to-zero, no idle floor.** A **SageMaker Batch Transform** job: the daily
  pipeline spins the instance up, scores the corpus of results-release / balance-commentary
  sentences, writes results to S3, and tears the instance down — billed only for the job. (An
  always-on real-time endpoint — the mode Rev. 1 wrongly assumed — is explicitly *not* used;
  Serverless Inference is the fallback if a low-latency on-demand path is ever needed.) *This ADR
  states the mechanism, not a dollar figure — no fabricated cost numbers; the actual per-run cost
  is to be measured on first run.*
- **Where it lands — the existing feature stage.** Output is a per-entity **financial-tone signal**
  (score + trend + the sentences that drove it, cited) folded into
  [`src/synth/feature_store.py`](../src/synth/feature_store.py) → `features/latest.json`, then into
  the soundness axis (§2). Because it is a feature every downstream reader already consumes, it
  **enriches every officer agent's grounded answers across every entity** — the stated goal —
  *alongside* (never replacing) the hard ratios of §1.
- **Boundary with Bedrock (both kept).** FinBERT produces the *numeric tone feature*; the existing
  **Bedrock (Nova/Claude) synth + managed KB** still produces the *grounded, cited narrative* and
  reads the **Pilar 3 / Relatório de Gerenciamento de Riscos** PDFs (chunked at ingest, processed
  incrementally across the DAG — never inside one 15-minute execution). Numeric tone and narrative
  are different jobs; each uses the right tool.
- **Honesty.** The tone score is labelled **inference**, carries its `_prov` (ADR 018) and the
  source document + as-of quarter, and is presented separately from the reported ratios — a model
  opinion, cited to the text it read, never dressed as a reported fact.

### 4. Cadence-matched scheduling — a separate **monthly** financial pipeline

The **monthly balancetes** (Tier B) are the genuinely monthly driver; the quarterly ratios
(Tier A) overlay on top. The main pipeline runs **3×/day** (`OncaPipeline`, three EventBridge cron
rules → the state machine). Folding §1 + §3 into that daily cadence would **reprocess unchanged
data ~90×/month** and pay the SageMaker Batch Transform cost daily for zero new signal. So they get
their own schedule, matched to the monthly balancete release.

- **A second state machine, `OncaFinancialsPipeline`**, on its **own monthly EventBridge rule**
  (`Schedule.cron` on a day-of-month a few days after month-end, so BCB has published the balancete):
  `bcb_soundness` ingest (Tier A quarterly ratios *when a new quarter exists* + Tier B monthly
  balancete → append the new trajectory point) → **FinBERT-PT-BR Batch Transform** → tone-feature
  merge. Isolated so the long SageMaker + heavy balancete parse live **outside the daily latency
  budget** and a financial-run failure never touches the 3×/day cycle.
- **Decoupled by the same S3-as-contract pattern the pipeline already uses.** The monthly run only
  *writes* durable stores (`soundness/index.json`, the per-entity financial-tone feature into
  `feature_store` / `features/latest.json`); the **daily** pipeline + feed-builder only *read* them.
  No execution coupling — if a monthly run is late or fails, the daily feed keeps serving the
  last-known stores, each point carrying its **as-of month/quarter** so staleness is visible, never
  silent, and the trajectory simply shows a gap rather than a fabricated value.
- **Cheap, idempotent, append-only re-runs.** The monthly job checks the latest published base
  month (a balancete analogue of `bcb_ifdata.latest_base_date`) against the last point already in
  `series[]` and **no-ops** if unchanged; when a new month exists it **appends one trajectory
  point** (never rewrites history). So a retry, a manual trigger, or a mis-timed month-end costs
  almost nothing, and the monthly series stays a clean audit trail.
- **Same deploy/orchestration idioms** as `OncaPipeline` (Step Functions + EventBridge cron +
  the ad-hoc "run soon" one-shot Scheduler), so there is no new operational model — just a second,
  slower cadence for slower-moving data.

## Consequences

**Pros.**
- Closes a first-order gap (competitor *soundness*, and the *non-listed* universe) using an
  integration Onça already operates — low marginal cost, ADR-019-shaped (one `SourceSpec`).
- Stays inside every accepted architectural decision: parallel DAG, managed KB, serverless
  Bedrock, `_prov`/no-fabrication, officer surfacing.
- Reuses the #7 financials store and #10 orchestration rather than standing up a parallel
  monolith beside them.

- Adds a **distinctive, cross-entity financial-tone signal** (§3) that enriches every officer
  agent's grounded answers over the *whole* entity universe — a dedicated, calibrated instrument
  the general-LLM path cannot match on consistency or cost-at-scale.
- **Cadence-matched cost** (§4): the monthly `OncaFinancialsPipeline` runs the heavy SageMaker step
  ~once a month (matching the data), not ~90×/month — the daily pipeline stays fast and cheap, and
  the two decouple cleanly through durable S3 stores.
- **Leading-indicator trajectory** (Tier B): a monthly balancete series turns soundness from a
  quarterly *snapshot* into a *direction* — rising PDD or deposit outflow surfaces months before the
  quarterly ratio moves, which is the difference between a warning and a post-mortem.

**Cons / risks.**
- **Entity resolution** is the hard part — thousands of institution names → registry ids; the
  existing resolver will leave a long tail null (acceptable: null over invented, and it seeds
  ADR-011 discovery of the unresolved names).
- **OData relatório shape drift** — BCB has renamed endpoints before (already handled defensively
  in `bcb_ifdata.latest_base_date`); the soundness fetch inherits that fragility and its retries.
- **Balancete volume + COSIF account mapping** — monthly balancetes are large per-institution
  numeric files, and the COSIF chart of accounts must be mapped correctly to the trajectory lines
  (crédito, PDD, depósitos, PL). This is the real work of Tier B: mis-mapping an account silently
  corrupts the slope. Mitigated by pinning a small, explicit account→line map (cited in-store) and
  validating the derived monthly total against the quarterly Tier-A figure where they overlap.
- **Band thresholds are regulatory-judgement**, not fact — `soundness_band` must cite the
  threshold it applied and stay separable from the raw published ratios.
- **New service class — SageMaker is the first in the stack.** The real cost of §3 is *operational
  surface*, not idle dollars: a Batch Transform job to define/monitor, a model artifact
  (FinBERT-PT-BR) to vendor and pin, and IAM/CDK wiring. Contained by keeping it a single
  scheduled batch step that writes to S3 like every other task, and by gating it behind a flag so
  the pipeline runs without it until it is proven.
- **FinBERT-PT-BR is a third-party model** — its label set and training corpus must be validated
  against Brazilian *results-release* language before its score is trusted; until then the tone
  feature ships **shadow** (computed, stored, not surfaced).

**Rejected alternatives.**
- *Single Lambda (ingest+embed+infer) — the "ADR 01" sketch.* Conflicts with #10; re-serialises
  ingest; 15-min ceiling on big PDFs. Rejected.
- *Own Parquet vector store on S3.* No ANN index; duplicates the managed KB (ADR 004). Rejected.
- *FinBERT on an always-on real-time endpoint.* Idle-cost floor. Rejected **in favour of Batch
  Transform** (§3) — same model, scale-to-zero.
- *Replacing Bedrock synthesis with FinBERT.* Different jobs — FinBERT gives a numeric tone
  feature, Bedrock gives the grounded cited narrative. Both kept. Rejected as an either/or.

## Phasing

1. **Tier A — quarterly ratios. SHIPPED + LIVE (2026-09-06, `a00882a` + fast-fail fix).**
   `bcb_soundness.py` reads Relatório 5 solvency off the existing Olinda client → durable
   `soundness/index.json` (latest level); `feed_builder` joins it onto `entities[].soundness`
   (Basileia / Tier 1 / CET1 / leverage / imobilização + band). 7 unit tests + 945 suite green;
   deployed to the ingest Lambda (gated `ONCA_SOUNDNESS`, **default OFF** — daily is not the home) +
   feed-builder. Store populated live for base 202603: **122 tracked entities** with real Basileia
   (micro-IPs with negative capital → frágil; XP/porto_seguro/mercado_pago/c6 → atenção; incumbents
   ~15% → sólido); **32 in-feed entities carry `soundness`**. Fast-fail hardening (cheap `$top=1`
   base-date probe, 3-try `_get`, `conglomerates_only` bounds resolution to the ~567 `- PRUDENCIAL`
   rows) after a hang on the flaky endpoint. Refresh is Phase 3's job (monthly pipeline) — quarterly
   data is stable until the next quarter publishes.
2. **Tier B — monthly balancete trajectory. SHIPPED + LIVE (2026-09-06).** `src/ingest/
   bcb_balancete.py` reads BCB's monthly bulk balancete CSV (doc 4010,
   `.../cosif/Bancos/{YYYYMM}BANCOS.csv.zip`; `;`/latin-1/decimal comma) with a **pinned COSIF
   account→line map** (crédito 1600000007, depósitos 4100000009, PL 6000000004, disponibilidades
   1100000002, PDD 1899600005+1899900004 — the map is cited in-store), resolves the institution name
   to a tracked entity, and **appends one point per month** to a durable `balancete/index.json`
   `series[]` (append-only; a stored `Índice de Imobilização`-style liquidity proxy noted as NOT the
   LCR). `trajectory()` = latest + MoM % per line. Runs as the **BalanceteTask** (2nd task on
   `OncaFinancialsPipeline`, `OncaBalancete` Lambda 1024 MB/10 min). 5 tests (958 green). Live store:
   **69 entities**, 3 months backfilled (202604–202606) → real MoM (e.g. Bradesco PDD −8.9%, BTG
   depósitos +6.3%). NB IF.data is quarterly-only (verified) so this is a genuinely separate monthly
   source; a `{"months":[…]}` handler override backfills history. **Feeds `feature_store`/
   `longitudinal` + the Phase-4 slope firing = remaining wiring** (store is live and accumulating).
3. **`OncaFinancialsPipeline` on a monthly EventBridge cron — SHIPPED + LIVE (2026-09-06,
   `dd468ff`).** A dedicated `OncaFinancials` Lambda (`bcb_soundness.lambda_handler`, 1024 MB, digests
   RW + entities read) → a `OncaFinancialsPipeline` Step Functions state machine (SoundnessTask +
   retry) → `OncaFinancialsScheduleMonthly` cron (6th, 06:00 UTC / 03:00 BRT, after month-end).
   Decoupled from `OncaPipeline` purely via S3 (writes `soundness/index.json`, daily feed reads it).
   **Base-month no-op guard** in `run()` (skip the whole fetch/resolve when the stored quarter ==
   latest published; `merge()` carries top-level `base_date`; `force=True` bypasses). `cdk deploy`d;
   verified live — run 1 SUCCEEDED (`ok`, base 202603, 118 mapped), run 2 SUCCEEDED (`noop`, "quarter
   unchanged"). Wraps steps 1–2 today; step 5 (FinBERT) joins as a second task later.
4. **Soundness surfacing — SHIPPED + LIVE (2026-09-06).** `executive.build_cro` derives a
   solvency view from `feed.entities[].soundness`: a **CRO "Solidez prudencial dos concorrentes"**
   band (weakest Basileia first, band chips + Tier 1/CET1), a "Solidez sob atenção" hero tile, a
   per-sector `min_basileia`/`n_weak_solvency` aggregate, and an immediate rec when a competitor is
   frágil/atenção. +1 test; headless-verified (0 JS errors); live feed carries
   `executive.cro.solvency` (25 rows). **Scope note:** this surfaces the *level + band* on one
   quarter of data; the deeper **belief-axis firing on a worsening slope** (a deteriorating Basileia
   as a SWOT Threat) waits on Phase 2's monthly balancete trajectory.
5. **Financial-tone feature (FinBERT-PT-BR) — SHADOW MODULE + LIVE STORE (2026-09-06); automated
   cloud compute DEFERRED.** `src/synth/financial_tone.py` computes a per-entity net tone
   (P(POS)−P(NEG) ∈ [−1,1]) from the solvency store, scored by FinBERT-PT-BR (`score_fn` injected;
   `finbert_score_fn()` = lazy transformers). Durable **shadow** store `financial_tone/index.json` +
   `tone_by_entity()`; every value `is_inference:True`, `corpus="solvency_facts"` (Phase 6 swaps to
   results-release/Pilar 3 text). 4 tests. **Shadow-first honoured** — nothing reads it (no feed
   join, no board). Live store computed by FinBERT over 122 entities (tone tracks the band: frágil
   micro-IPs ≈ −0.38, sólido ≈ +0.61) and uploaded. **The automated monthly compute** (a 2nd
   `OncaFinancialsPipeline` task) is deferred: it needs a container image or a SageMaker HuggingFace-
   DLC Batch Transform — this environment has no usable docker and a zip Lambda can't hold
   torch+model (~2 GB). The module is written to be that task's handler body; the live store was
   populated via a one-shot local FinBERT run.

   **Update (2026-09-06) — FULLY REALIZED (automation + endpoint LIVE).** The FinBERT-PT-BR endpoint
   is provisioned and the automated compute runs end-to-end. `financial_tone.run/lambda_handler` +
   `sagemaker_score_fn()` invoke a **scale-to-zero SageMaker HuggingFace-DLC Serverless Inference
   endpoint** (`onca-finbert-ptbr`, 3072 MB — no idle cost, no docker) running
   `lucas-leme/FinBERT-PT-BR`; wired as **ToneTask** (3rd on `OncaFinancialsPipeline`,
   soundness→balancete→tone), `ONCA_FINBERT_ENDPOINT` set via CDK. **Verified live:** the full 3-task
   pipeline runs green and `financial_tone/index.json` = **122 entities computed by FinBERT via the
   endpoint** (tracks the band: frágil ≈ −0.38, sólido ≈ +0.61). Still **shadow** — nothing surfaces
   it. Reproducible via `scripts/deploy_finbert_endpoint.py`.

   *The earlier blocker, resolved:* it was never a Python-3.14 incompatibility — the SageMaker SDK's
   transitive `python-rapidjson` simply had no cp314 wheel and compiled from source needing `Python.h`
   (fixed by extracting the `python3.14-dev` .deb + `CPLUS_INCLUDE_PATH`, no sudo). And the deploy API
   needs **SageMaker SDK v2** (`pip install "sagemaker<3"` — v3 dropped `image_uris`/`HuggingFaceModel`).
   Role: `OncaSageMakerFinBERT`.
6. **Pilar 3 / risk-report PDF ingest — TONE-CORPUS HALF SHIPPED + LIVE (2026-09-06); KB-grounding
   half remains.** `src/ingest/pilar3.py` discovers Pilar 3 (*Relatório de Gerenciamento de Riscos*)
   filings via **CVM IPE** (`Assunto` carries "Gerenciamento de Riscos" + "Pilar"; direct
   rad.cvm.gov.br `Link_Download`; ENET mislabels content-type but the body is a real `%PDF`),
   extracts cleaned risk-narrative prose → durable `pilar3/index.json`. `financial_tone` now reads
   this real text per entity (`corpus="pilar3"`) instead of the fact-paraphrases — **the tone is no
   longer circular.** Live: Itaú tone 0.265 from its real 2T26 filing (via the SageMaker endpoint).
   **Coverage:** IPE covers Itaú today; other banks file Pilar 3 on IR sites (mziq/own CDN) — separate
   fetchers, those entities stay `solvency_facts` (labelled). **Remaining:** feed the full Pilar 3
   text into the Bedrock KB for grounded Q&A (the officer-retrievable half).

**Cross-phase (2026-09-06):** **Phase 4 slope firing** now LIVE — the Tier-B monthly PDD slope feeds
the CRO axis: a competitor whose provisions rise ≥5% MoM flags `slope_warning` + an immediate
"Deterioração de crédito" rec (live: XP flags on *both* weak Basileia 11.93% **and** PDD +6.5%/mo).
**Tone surfaced** (was shadow) — `feed.entities[].financial_tone` + the CRO solvency rows show
"tom ±N (Pilar 3 | inf.)", labelled inference with its corpus. **ADR 022 Phases 1–6 now materially
LIVE** (Phase 6 KB-grounding half + multi-bank Pilar 3 coverage the only remainders).

## Revision note

**Rev. 1 → Rev. 2 (2026-09-06).** Rev. 1 rejected FinBERT outright on two grounds — an "always-on
idle-cost floor" and "English vs pt-BR." Both were wrong for the intended use: SageMaker **Batch
Transform** (and Serverless Inference) **scale to zero**, so a daily batch job has no idle cost; and
**FinBERT-PT-BR** is Brazilian-Portuguese. The durable point survives — FinBERT is not a *narrative*
tool and does not replace Bedrock synthesis — but as a **deterministic financial-tone feature** over
Resultados e Balanços, run scale-to-zero and folded into the existing feature stage, it is a genuine,
distinctive enrichment across every entity. Reinstated as §3.

## Addendum (2026-09-06) — Financial-statement dimensions roadmap (Resultados e Balanço)

With the monthly balancete (balance sheet **+** P&L: COSIF 7xxx rendas / 8xxx despesas), the IF.data
summary (Ativo, Carteira, Depósitos, Captações, **Lucro Líquido**, PL) and the Basileia set all
flowing, a large set of new dimensions is derivable. Tabled by feasibility, ease and the officer who
benefits most.

| # | Dimension | Measures | Data status | Ease | Primary officer |
|---|---|---|---|---|---|
| 1 | ROE / ROA | Lucro ÷ PL / ÷ Ativo | have (IF.data) | trivial | **CSO** |
| 2 | Loan-to-Deposit (LDR) | Carteira ÷ Depósitos | have | trivial | CRO |
| 3 | Leverage / funding mix | Ativo÷PL; depósitos vs captações | have | trivial | CRO |
| 4 | Capital headroom | Basileia − mínimo (buffer) | have | trivial | **CSO** / CCO |
| 5 | Share-by-metric | share of crédito/depósitos/lucro | have | easy | CSO |
| 6 | Índice de eficiência | Despesas Adm ÷ Receitas | balancete (extract) | easy | **CSO** |
| 7 | NIM / spread | (rendas − despesas de captação) ÷ ativos rentáveis | balancete | easy | CSO/CPO |
| 8 | Custo de crédito | Despesas de PDD ÷ Carteira | balancete (have PDD) | easy | CRO |
| 9 | **Inadimplência / NPL** | carteira D–H ÷ total | need IF.data Rel. 8 | medium | **CRO** ⭐ |
| 10 | Cobertura de provisões | PDD ÷ créditos problemáticos | Rel. 8 + PDD | medium | CRO |
| 11 | Composição da carteira | PF/PJ, modalidade, CNAE | need Rel. 7/11–16 | medium | **CPO** |
| 12 | Concentração de crédito | top-N / setorial | Rel. 12/14 | medium | CPO/CRO |
| 13 | Liquidez estrutural (proxy) | (disponib.+TVM) ÷ passivo — NOT the LCR | have | easy | CRO |
| 14 | Peer benchmarking / percentis | rank vs peers on every ratio | derived | medium | **CSO** |
| 15 | Valuation (P/L, P/VP) | market cap ÷ lucro/PL | quotes+financials (listed) | medium-hard | CSO |
| 16 | Composite fragility score | Basileia↓+PDD↑+ROE↓+LDR↑ | derived | hard | **CRO** |
| 17 | Tone-vs-numbers divergence | FinBERT tone vs real ROE/NPL trajectory | derived | hard | CRO/CSO |

**Caveats.** Balancete P&L (7xxx/8xxx) is **year-accumulated** (resets in January) — MoM/annualisation
needs YTD-aware handling (#6/#7/#8 and ROE). Rows 9–12 need new IF.data relatórios (same Olinda client,
different `Relatorio` code — the soundness recipe again). #15 is listed-only and needs a reliable
market-cap join.

**Where value concentrates.** **CSO** gains the most net-new — today it has no *financial* strength
axis (narrative/momentum only); ROE/efficiency/headroom/benchmarking give it one. **CRO** deepens with
inadimplência/NPL (#9) — the highest-value single number, named in §1 and still unbuilt. **CPO** gets
product-mix intelligence from carteira composition (#11).

### Recommended sequencing (this addendum drives the next builds)
1. **Tier 1 — CSO financial axis. SHIPPED + LIVE (2026-09-06).** `src/ingest/bcb_fundamentals.py`
   (one IF.data Rel. 1 fetch) → ROE/ROA (annualised, YTD-aware), alavancagem, crédito÷captações,
   capital headroom, share of crédito/lucro → `fundamentals/index.json` (runs in the monthly
   OncaFinancials Lambda, folded into the soundness handler). `feed_builder` joins
   `entities[].fundamentals`; `build_cso` adds a "Força financeira dos concorrentes" panel +
   avg_roe/n_negative_roe aggregates + two recs (profitability leader as benchmark; loss-maker as
   fragility thesis); v3 CSO board renders it. Live (122 entities): Itaú 21% of sector profit, XP
   ROE −1.2% (weak on a 3rd axis). +5 tests (970 green). **Known edge:** insurers appear via their
   bank arm with a tiny IF.data asset base → an inflated ROA (e.g. SulAmérica) — labelled inference;
   a per-metric sanity guard is a follow-up.
2. **Tier 2 — Inadimplência/NPL. SHIPPED + LIVE (2026-09-06).** IF.data **Rel. 8** (nível de risco
   AA–H) is NOT exposed via OData (verified — 0 rows every quarter/tipo), so NPL is computed from
   **Rel. 11 (PF) + Rel. 13 (PJ)** — each carries a *"Vencido a Partir de 15 Dias"* overdue bucket +
   portfolio Total: `inadimplência = Σ vencido15+ ÷ Σ total`, with the PF/PJ split (labelled **15+
   dias**, broader than the 90+ standard). `src/ingest/bcb_inadimplencia.py` → `inadimplencia/index.
   json` (runs in the monthly OncaFinancials Lambda). Joined onto the **CRO** solvency rows (NPL +
   PF/PJ + band) with an "Inadimplência elevada" rec (band elevada ≥7%) + per-sector
   max_npl/n_npl_elevada. The CRO now has the full credit-risk picture per competitor: Basileia
   (capital) · PDD slope (provisioning trend) · NPL (actual delinquency) · tone. Live (63 entities):
   Nubank 12.93% elevada, Inter 4.78% atenção, Itaú 1.98% (prime). +5 tests (974 green). **Known
   edge:** small/atypical credit books yield extreme ratios (e.g. SumUp 40.9%) — inference-labelled,
   same class as the Tier-1 insurer-ROA edge; a small-book/denominator guard is a shared follow-up.
3. **Tier 3 — operating efficiency. SHIPPED + LIVE (2026-09-06).** `src/ingest/bcb_resultados.py`
   computes **custo operacional / ativo** = annualised admin+pessoal opex (COSIF `8170000004`, YTD
   ×12/month) ÷ ativo (`1000000009`) from the monthly balancete P&L → `resultados/index.json` (monthly
   OncaFinancials Lambda); merged onto the **CSO** financial rows as `opex/ativo%`. Validated against
   reality (Itaú 1.54%, matches). +4 tests (977 green). **Honestly de-scoped:** shipped only the
   validated ratio. **Custo de crédito** from 4010 came out **~2× known figures** (the gross
   despesa/reversão gross-up overstates net PDD) → NOT shipped; it's deferred to the DRE (doc 4016),
   and the CRO already reads credit cost via NPL (Tier-2) + the PDD slope (Tier-B), so it's redundant
   there. A textbook **cost-to-income** needs the DRE's net-income denominator — also deferred.
4. **Later/optional:** composite fragility (#16), valuation (#15), tone-vs-numbers (#17).
