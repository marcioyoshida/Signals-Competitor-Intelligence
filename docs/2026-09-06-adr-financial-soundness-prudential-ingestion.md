# ADR 022 — Financial-Soundness (Prudential) Ingestion for BCB/CVM Institutions

Status: **PROPOSED** 2026-09-06.

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

**What the sketch got wrong** (each conflicts with an accepted ADR):

1. **One Lambda does ingest + embed + infer in a single execution.** The pipeline is a
   *deliberately parallelized* Step Functions DAG (#10): `sfn.Parallel` ingest fan-out →
   feature → synth → silence/longitudinal/comparative/thematic → feed, tasks coordinating
   only via S3. Re-monolithising re-serialises the ingest bottleneck and would hit the
   15-minute ceiling **exactly** on the large-PDF case the sketch itself flags as its risk.
2. **Write vectors to S3 as Parquet, "eliminating active databases."** Onça *already* has no
   active vector DB — retrieval is a **managed Bedrock Knowledge Base** (S3 Vectors, KB
   `CQ5LBZBQTY`) for RAG plus an in-process cosine over **Titan Embed v2**
   ([`src/synth/embeddings.py`](../src/synth/embeddings.py)) for bounded SWOT reconcile.
   Hand-rolled Parquet vectors have **no ANN index** (a full scan per query) and duplicate a
   solved problem.
3. **A FinBERT endpoint for sentiment.** Sentiment/synthesis already runs on **serverless
   Bedrock (Nova/Claude)**. A SageMaker-hosted FinBERT is an always-on **idle-cost floor** —
   the very thing the sketch claims to avoid — and FinBERT is English-trained against pt-BR
   risk reports.

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

### 3. Risk-report reading via the **existing** Bedrock + KB path — no FinBERT

Banks' **Pilar 3 / Relatório de Gerenciamento de Riscos** PDFs are a genuine text source. Read
them the way Onça reads every other document:

- Ingest the PDF text as a raw doc → the **existing synth** (Nova/Claude Converse) produces a
  grounded, cited risk-posture narrative under the existing anti-fabrication + citation
  guardrails; **Titan Embed v2 → the managed KB** makes it retrievable by the officer agents.
- Large PDFs are chunked at **ingest** and processed incrementally across the DAG — they never
  sit inside one 15-minute execution (the sketch's own stated failure mode).

## Consequences

**Pros.**
- Closes a first-order gap (competitor *soundness*, and the *non-listed* universe) using an
  integration Onça already operates — low marginal cost, ADR-019-shaped (one `SourceSpec`).
- Stays inside every accepted architectural decision: parallel DAG, managed KB, serverless
  Bedrock, `_prov`/no-fabrication, officer surfacing.
- Reuses the #7 financials store and #10 orchestration rather than standing up a parallel
  monolith beside them.

**Cons / risks.**
- **Entity resolution** is the hard part — thousands of institution names → registry ids; the
  existing resolver will leave a long tail null (acceptable: null over invented, and it seeds
  ADR-011 discovery of the unresolved names).
- **OData relatório shape drift** — BCB has renamed endpoints before (already handled defensively
  in `bcb_ifdata.latest_base_date`); the soundness fetch inherits that fragility and its retries.
- **Band thresholds are regulatory-judgement**, not fact — `soundness_band` must cite the
  threshold it applied and stay separable from the raw published ratios.

**Rejected alternatives.**
- *Single Lambda (ingest+embed+infer) — the "ADR 01" sketch.* Conflicts with #10; re-serialises
  ingest; 15-min ceiling on big PDFs. Rejected.
- *Own Parquet vector store on S3.* No ANN index; duplicates the managed KB (ADR 004). Rejected.
- *FinBERT endpoint.* Idle-cost floor; en-vs-pt-BR mismatch; Bedrock already does grounded
  sentiment. Rejected.

## Phasing

1. `bcb_soundness.py` — soundness relatórios off the existing Olinda client → `soundness/index.json`,
   merged into the financials read + competitor cards (Basileia, inadimplência, ROE, band).
2. Soundness **belief axis** → SWOT/frameworks + CRO/CPO panels.
3. Pilar 3 / risk-report PDF ingest → existing synth + KB (grounded, cited); officer-retrievable.
4. *(Optional)* COSIF micro-account fallback only if the pre-computed relatórios prove insufficient.
