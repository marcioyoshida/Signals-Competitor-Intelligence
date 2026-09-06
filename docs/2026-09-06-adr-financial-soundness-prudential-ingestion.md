# ADR 022 — Financial-Soundness (Prudential) Ingestion + Financial-Tone NLP for BCB/CVM Institutions

Status: **PROPOSED** 2026-09-06. **Rev. 2 (2026-09-06)** — FinBERT reinstated as a
scale-to-zero *feature-extraction* stage (Batch Transform, FinBERT-PT-BR) after review;
the Rev. 1 blanket rejection conflated SageMaker with an always-on endpoint. See
*Revision note* and §3.

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

Not a new Lambda. A new source under the ADR-019 registry, run inside the existing
`structured` ingest branch of the parallel fan-out.

- **Source.** The **same Olinda OData service** `bcb_ifdata` already calls — the prudential
  relatórios are just different `Relatorio` codes on that client (Resumo prudencial with
  Índice de Basileia; carteira-de-crédito relatório with inadimplência). So this is an
  *extension of an existing integration*, not a new one. COSIF micro-accounts stay a Phase-2
  option only if the OData relatórios prove insufficient — most soundness ratios are already
  published pre-computed.
- **Indicators (all pre-computed by BCB or a ratio of two reported lines — never invented):**
  `basileia_pct`, `inadimplencia_pct` (NPL), `roe_pct`, `roa_pct`, `liquidez`, plus a coarse
  `soundness_band` (sólido / atenção / frágil) derived from published regulatory thresholds
  (e.g. Basileia < 11% → atenção). Every field carries its `_prov` (ADR 018) and the
  as-of quarter; unresolved institutions stay **null**, never a fabricated number
  (CLAUDE.md no-unlabeled-proxy rule, same discipline as `bcb_ifdata`).
- **Resolution + store.** Reuse `bcb_reclamacoes.map_to_entities` name→`entity_id` resolution
  and persist a durable `soundness/index.json` (same shape as `bcb_ifdata/index.json`), merged
  into the #7 `financials/index.json` read so a competitor card shows *size, growth AND
  soundness* in one place. Covers the **non-listed** universe the CVM path cannot reach.

### 2. A financial-soundness **belief axis** (ADR 003), not a new inference stage

Soundness is surfaced through the mechanisms that already exist:

- A new axis feeds SWOT/frameworks: a deteriorating Basileia or rising inadimplência at a
  competitor is a **Strength** for us / **Threat** to them (mirrors the crédito-&-inadimplência
  thematic current already in [`src/synth/thematic.py`](../src/synth/thematic.py)).
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

**Cons / risks.**
- **Entity resolution** is the hard part — thousands of institution names → registry ids; the
  existing resolver will leave a long tail null (acceptable: null over invented, and it seeds
  ADR-011 discovery of the unresolved names).
- **OData relatório shape drift** — BCB has renamed endpoints before (already handled defensively
  in `bcb_ifdata.latest_base_date`); the soundness fetch inherits that fragility and its retries.
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

1. `bcb_soundness.py` — soundness relatórios off the existing Olinda client → `soundness/index.json`,
   merged into the financials read + competitor cards (Basileia, inadimplência, ROE, band).
2. Soundness **belief axis** → SWOT/frameworks + CRO/CPO panels.
3. **Financial-tone feature** — FinBERT-PT-BR on a SageMaker Batch Transform step → per-entity tone
   in `feature_store` → soundness axis + agent grounding. Ships **shadow** (flag-gated, computed &
   stored, not surfaced) until validated against pt-BR results-release language and its per-run cost
   is measured; then flipped on.
4. Pilar 3 / risk-report PDF ingest → existing synth + KB (grounded, cited); officer-retrievable.
5. *(Optional)* COSIF micro-account fallback only if the pre-computed relatórios prove insufficient.

## Revision note

**Rev. 1 → Rev. 2 (2026-09-06).** Rev. 1 rejected FinBERT outright on two grounds — an "always-on
idle-cost floor" and "English vs pt-BR." Both were wrong for the intended use: SageMaker **Batch
Transform** (and Serverless Inference) **scale to zero**, so a daily batch job has no idle cost; and
**FinBERT-PT-BR** is Brazilian-Portuguese. The durable point survives — FinBERT is not a *narrative*
tool and does not replace Bedrock synthesis — but as a **deterministic financial-tone feature** over
Resultados e Balanços, run scale-to-zero and folded into the existing feature stage, it is a genuine,
distinctive enrichment across every entity. Reinstated as §3.
