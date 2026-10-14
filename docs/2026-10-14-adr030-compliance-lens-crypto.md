# ADR 030 — Compliance lens: PSAV counterparty radar and stablecoin attestation monitor

- Status: **Proposed** — 2026-10-14. Owner-requested: *"Add this as ADR for Compliance lens"*,
  following a research session on what the low-cost ingestion stack (S3 / S3 Vectors / Bedrock)
  can do with open crypto data, and where nothing is launched yet.
- Per **ADR 024**, a Proposed-only ADR is roadmap, never demo. Nothing here pre-empts the five
  launch blockers (#109–#113). It is a candidate **design-partner hook** because of the
  6 November date below, not a launch dependency.
- Builds on: ADR 009 (regulatory change intelligence — the `regdocs/` versioned store), ADR 019
  (declarative source & lens registry), ADR 021 (officer dashboards — the **CCO** board), #193
  (`synth/enforcement.py`, the CCO enforcement register), #195 (AML compliance tags in
  `federal_acts`).
- Out of scope, routed elsewhere: a crypto **litigation / enforcement corpus** belongs to Signals
  Legal Intelligence (different buyer: lawyers, investigative journalists). **DAO governance
  summarisation** is rejected (see Alternatives).

## Context

### The regulatory clock (facts as published; re-verify before any customer-facing claim)

| Date | Event | Source |
| --- | --- | --- |
| 2026-02-02 | Resoluções BCB 519/520/521 in force: PSAV services, SPSAV authorization and operation, crypto as câmbio / capitais internacionais. | bpmoney; okai |
| 2026-03-01 | Res. BCB 548 in force: maximum BCB decision windows — up to 1,080 days for PSAVs already operating, 2 years for new ones. | Capital Aberto; IstoÉ Dinheiro |
| 2026-07-01 | Receita **DeCripto** (IN RFB 2.291/2025) replaces IN 1.888/2019, aligned with the OECD CARF. | Jornal Contábil; Rota da Jurisprudência |
| 2026-10-01 | PLD/FT changes in force. Self-custody wallet transfers ≥ US$ 10k become a specific Coaf communication. | BCB note (via Poder360); Capital Aberto |
| 2026-10-30 | Final date for PSAVs already operating to file their authorization request. Filing starts the review; it does not end it. | Capital Aberto |
| **2026-11-06** | **Res. BCB 589:** from this date, BC-authorized institutions may not carry out or enable virtual-asset operations whose counterparty is an unauthorized provider (except where the rule allows). | Capital Aberto |
| 2027-01-01 | Res. BCB 550: virtual assets at fair value at least monthly; mandatory segregation of own vs client assets. | Capital Aberto |

Also in force: Res. CMN 5.280/26 brings PSAVs under LC 105/2001 (bank secrecy duties). PSAVs send
daily client-balance/custody data and **monthly proof of reserves** to the BCB (docs 5710/5711).
Those reports go to the supervisor and are **not public**, so they are not a source for this ADR.

On the US side, the **GENIUS Act** (July 2025) requires permitted payment stablecoin issuers to
publish monthly reserve reports, examined by a registered public accounting firm and certified by
the CEO/CFO. Today issuers publish at different cadences and lapses happen: NYDIG reported that, as of early October, USD1's
latest available attestation was from July (year not stated in the syndicated copy; verify).

### What the 6 November date does to Onça's buyer

From 6 November, every bank, payment institution and fintech in the watchlist must be able to
answer, per counterparty: **"Is this provider authorized, in process, or neither — and as of
when?"** That is a compliance-officer question with a dated legal consequence, i.e. exactly the
CCO board's job. The answer changes over the BCB's review windows (up to three years), so it is a
feed, not a one-off list.

### What already exists in this repo (verified 2026-10-14)

| Piece | Where | State |
| --- | --- | --- |
| `crypto` industry topic: DOU phrases (*ativos virtuais*, *criptoativos*), news queries, vocabulary incl. *psav*, *lei 14.478* | `src/ingest/registry.py` (`IndustryTopicSpec("crypto", …)`) | Live |
| PSAV coverage segment | `src/ingest/reg_coverage.py` (`Segment("CVM", "psav-cripto", …)`) | Declared, **"no registrant source → #14"** |
| AML/CFT as a cross-industry compliance tag (crypto is in `AML_DOMAIN`'s industries) | `src/ingest/federal_acts.py`, `src/synth/regulatory.py` (#195) | Live |
| COAF / UIF acts from the DOU | `src/ingest/dou.py` | Live |
| CCO enforcement register (supervisor acts against one operator; resolver-only entity binding) | `src/synth/enforcement.py` (#193) | Live |
| BCB institutions-in-operation registry (new-entrant warning) | `src/ingest/bcb_autorizacoes.py` | Live for banks, societies, co-ops, consórcios. **Whether SPSAVs appear in any EntitySet is unknown.** |
| Versioned full-text document store + section diff | `src/ingest/reg_documents.py`, `src/synth/reg_diff.py` (ADR 009) | Live |
| Crypto trade press (CriptoFácil etc.) | `src/ingest/trade_press.py` | Live (news-only) |

So Onça already **reads the rules** about PSAVs. What it lacks is the **entity-bound status
layer**: which named PSAV is where in the authorization process, and which stablecoin issuer has
published what, when.

### Open data that fits the stack (and how)

- **Documents → S3 Vectors + Bedrock KB:** BCB normativos and Atos (already ingested), DOU
  authorization acts, issuer attestation PDFs, Receita DeCripto acts.
- **On-chain → Athena / S3 Tables, never vectors:** the AWS public bucket
  `s3://aws-public-blockchain` (Bitcoin and Ethereum maintained by AWS; Arbitrum, Aptos, Base,
  Provenance and XRPL maintained by SonarX; TON and Cronos). Parquet, date-partitioned, readable
  with `--no-sign-request`. Caveats: AWS says the data is not for production use, and users have
  reported the Ethereum `blocks` set lagging ~20 days behind. Numbers do not embed usefully, so
  this data never enters the KB.
- **Stablecoin supply:** DeFiLlama's public stablecoins data is what existing comparisons use. It
  is an aggregator, so it is a *secondary* figure, labelled as such.

## Decision

Add a **`compliance` lens** to the ADR 019 registry for **entity-bound compliance status events**,
fed by two new sources, delivered to the **CCO** board. Rules and acts keep flowing through the
existing `regulatory` / `dou` lenses with their #195 compliance tags; the new lens carries only
*"this named entity's compliance status changed"* events.

### 1. Lens policy

```python
"compliance": LensSpec("compliance", 0.30, solo=True, structured_subject=True),
```

- `solo=True`: a single new status event (PSAV authorized, request published, attestation late)
  can surface on its own — same reasoning as `sanctions` (0.32) and `fatos` (0.30).
- `structured_subject=True`: it fuses only on a shared resolved entity, never on topic overlap.
  A PSAV status event must not merge into a generic "cripto" news cluster.
- Weight sits just under `sanctions` (0.32): a sanction is an adverse supervisory act; a status
  event is often neutral or positive.

### 2. Source A — `psav_status` (PSAV authorization & counterparty radar) — Phase 1

**Event model** (one record per PSAV per status change; durable store `compliance/psav/`):

| Field | Meaning |
| --- | --- |
| `entity_id` | Bound via the resolver (CNPJ root / aliases) only — same rule as CEIS/CNEP and #193. Unbound rows are kept, shown as the name the source prints, never fuzzy-matched. |
| `status` | One of `authorized`, `request_published`, `denied_or_cancelled`, `wind_down`. **There is no `unauthorized` value** (see honesty rules). |
| `as_of` | The publication date of the source act/list, not the fetch date. |
| `sources[]` | Official first (BCB list / DOU act URL), news only as `reported`. |
| `classification` | `intermediária` / `custodiante` / `corretora` when the source states it (Res. 520). |

**Candidate inputs, in order of preference — all to be verified in Phase 0 (CLAUDE.md: no
invented schemas):**

1. A BCB-published list of authorized SPSAVs and/or of requests under review (endpoint, format and
   existence unverified).
2. `bcb_autorizacoes`: check whether SPSAVs appear in an existing `Instituicoes_em_funcionamento`
   EntitySet once authorized; if so, the new-entrant detector already emits the event and only the
   classification is new.
3. DOU / BCB Atos: authorization, denial and cancellation acts naming the institution. `dou.py`
   already searches *ativos virtuais*; Phase 1 adds an act-type parser in the style of
   `federal_acts.enforcement_of`.

If none of 1–3 yields a request-under-review signal, the radar ships with `authorized` and
`denied_or_cancelled` only, and says so in the panel. It never fills the gap with news inference.

**Honesty rules (non-negotiable — defamation / LGPD exposure, same discipline as #193):**

- **Absence is not a status.** Not finding a PSAV in a list renders as *"Sem registro público
  encontrado em <fontes> até <data>"*, never as "não autorizada".
- Every status line shows `as_of` and its source link. A status older than the source's known
  refresh cadence is shown as stale.
- The panel carries a fixed disclaimer: it is a monitoring aid over public sources, not a legal
  opinion, and does not replace the institution's own due diligence under Res. 589.

### 3. Source B — `stablecoin_attestations` (attestation monitor) — Phase 2

- **Store:** reuse the ADR 009 pattern — `attestations/<issuer>/<content-hash>.pdf|txt` plus
  `attestations/index.json`, content-hash cached so re-fetches are free.
- **Issuers (initial):** those with public transparency pages and material Brazilian usage — USDT
  (Tether), USDC (Circle), PYUSD / USDP / USDG (Paxos), RLUSD (Ripple), USD1, FDUSD. Exact URLs
  and PDF links verified in Phase 0; no login-gated pages (CLAUDE.md).
- **Extraction:** bounded Bedrock call (nova-lite, as in `reg_change_record`) extracts
  `period_end`, `tokens_outstanding`, `reserve_total`, `composition[]`, `attestor`, `standard`.
  Grounding discipline from ADR 009 §3: the LLM only extracts; a deterministic check rejects the
  record if the composition lines don't sum to `reserve_total` within rounding, and every figure
  keeps a page reference.
- **Detectors (deterministic, no LLM):**
  - `attestation_late` — `today − period_end` exceeds the issuer's own declared cadence + grace.
  - `attestor_changed` — attestor firm differs from the previous report.
  - `composition_shift` — any reserve class moves by more than a threshold (watchlist knob).
  - `coverage_thin` — `reserve_total / tokens_outstanding` below 1.00 (or below the issuer's
    stated buffer).
- **Delivery:** CCO board "Stablecoins" panel; relevant to treasury/risk users holding stablecoins
  and to PSAVs choosing which coins to list.

### 4. Source C — on-chain supply reconciliation — Phase 3 (optional)

- Compare each attestation's `tokens_outstanding` with on-chain supply at `period_end`.
- Primary supply figure: DeFiLlama, labelled **"agregador (secundário)"**. Independent cross-check:
  Athena over `s3://aws-public-blockchain` for the chains it covers (Ethereum, Base, Arbitrum,
  XRPL), reading token transfer / mint-burn data for the issuer's known contracts — **table
  availability and schema per chain to be verified in Phase 0**.
- Because of the freshness caveat above, this runs monthly against `period_end`, never as a live
  number. Any reconciled figure is labelled estimated (CLAUDE.md).

### 5. KB and retrieval

- Attestation full texts and PSAV acts go into the existing Bedrock KB on S3 Vectors so `/api/ask`
  can answer *"o que mudou na composição de reservas do USDC desde julho?"* with citations.
- Filterable metadata stays within the S3 Vectors limits (≤ 1 KB, ≤ 35 keys): `tenant`, `doc_id`,
  `source`, `issuer_or_entity`, `period_end` as an integer `YYYYMMDD`. Everything else lives in the
  durable store, keyed by `doc_id`.

### 6. Cost (estimated — label kept per CLAUDE.md)

| Item | Volume | Estimate |
| --- | --- | --- |
| S3 Vectors storage + PUTs | a few thousand chunks a month | well under US$ 1/month at ~US$ 0.06/GB-month storage, ~US$ 0.20/GB PUT |
| Bedrock extraction (nova-lite) | ~10–20 issuer PDFs + a few dozen acts a month | single-digit US$/month |
| Athena (Phase 3) | monthly, partition-pruned to `period_end` dates | cents to low single-digit US$/month (verify scanned bytes in Phase 0) |
| New managed services | none | US$ 0 idle floor |

Well inside the ~US$ 100/month prototype ceiling; no idle-floor service is added.

## Phases

| Phase | Scope | Exit criterion |
| --- | --- | --- |
| **0 — Verification spike** (2–3 days) | Live-call every candidate source: BCB SPSAV lists, `Instituicoes_em_funcionamento` EntitySets for SPSAVs, DOU act wording for PSAV authorization/denial, issuer transparency URLs, DeFiLlama response shape, `aws-public-blockchain` region and per-chain tables. Record findings in `docs/DATA_SOURCES.md`. | Each source marked verified / unavailable with a dated live call. Go/no-go per source. |
| **1 — PSAV radar** | `psav_status` SourceSpec + `compliance` LensSpec, durable store, resolver binding, CCO "Contrapartes cripto" panel, SNS alert on status change. Closes the `psav-cripto` "no registrant source" gap in `reg_coverage`. | 3 consecutive pipeline runs with correct deltas; honesty rules covered by tests (absence ≠ status; `as_of` always present). |
| **2 — Attestation monitor** | `stablecoin_attestations` store, extraction + sum check, four detectors, CCO "Stablecoins" panel, KB ingestion. | Two monthly cycles extracted for every initial issuer with zero sum-check failures shipped to the feed. |
| **3 — Reconciliation** (optional) | DeFiLlama + Athena supply cross-check. | Reconciled figures labelled estimated; drift alerts only above a threshold. |
| **4 — Counterparty API** (later) | `psav_status` exposed as a tool on the metered agentic API (ADR 025) for bank onboarding systems. | Priced per ADR 024/025. |

Phase 1 is the one with a deadline: it is most useful as a design-partner conversation **before
6 November 2026**, and remains useful afterwards because BCB reviews run for up to three years.

## Who it serves

- **Primary payer:** compliance and counterparty-risk teams at banks, payment institutions and
  fintechs (Onça's existing buyer), obliged by Res. 589.
- PSAVs themselves (own and competitors' authorization progress), law firms and consultancies
  advising PSAVs, auditors preparing for Res. 550.
- Phase 2: treasury/risk teams holding stablecoins, exchanges and PSAVs deciding what to list,
  institutional risk analysts.
- Benefits but does not pay: retail investors (indirectly safer counterparties).

## Alternatives considered

1. **DAO governance summarisation.** Rejected: crowded — Messari Governor, Blockworks GovHub,
   Cardano Proposal Examiner, Polkassembly's Klara, plus free bots. No Brazilian-buyer angle.
2. **Fold PSAV status into the existing `regulatory` lens.** Rejected: the regulatory lens is
   topic-clustered (rules), and a per-entity status would fuse into generic "cripto" clusters —
   the #50 mis-clustering class ADR 019 exists to prevent.
3. **Put on-chain data into the KB.** Rejected: numeric ledgers embed poorly and would cost PUTs
   for no retrieval value; Athena answers exact questions exactly.
4. **Paid on-chain / attestation data vendors.** Deferred: breaks the free-government-tier MVP rule
   and the cost ceiling; revisit only if a design partner needs real-time data.

## Consequences

**Positive**
- Closes a declared coverage gap (`psav-cripto`, #14) with an entity-bound source.
- Gives the CCO board a dated, legally-motivated panel instead of complaint rankings alone.
- Reuses ADR 009's store/diff and ADR 019's registry; no new managed service.

**Negative / risks**
- **Source risk:** if the BCB publishes no machine-readable SPSAV list, Phase 1 depends on DOU
  act parsing and may only cover authorizations, not pending requests.
- **Legal exposure:** a wrong status about a named company is defamation/LGPD risk. Mitigated by
  the honesty rules, resolver-only binding and the fixed disclaimer, but it needs a review pass
  before any external tenant sees the panel.
- **Freshness:** the public blockchain bucket is experimental and has lagged; Phase 3 is monthly
  and optional for that reason.
- **Scope creep:** Phase 2 is US-issuer-centric; keep it tied to coins with Brazilian volume.

## Open questions

1. Does the BCB publish (or will it, after 30 October) a list of SPSAV requests under review? If
   only authorized entities are listed, is that still enough for the Res. 589 use case?
2. Do bank and fintech PSAVs authorized through the separate procedure for already-authorized
   institutions need their own status value?
3. Should `denied_or_cancelled` also feed `synth/enforcement.py`, or stay compliance-only?
4. Is the CCO board the only surface, or does the CRO need the stablecoin panel too?

## Sources

- Capital Aberto — [Prazo do BC aperta cerco sobre ativos virtuais](https://capitalaberto.com.br/regulacao/2026/10/prazo-do-bc-aperta-cerco-sobre-ativos-virtuais) (Res. 589, 30 Oct / 6 Nov, Coaf US$ 10k)
- Capital Aberto — [Novo marco regulatório e contábil das PSAVs](https://capitalaberto.com.br/artigos/2026/03/novo-marco-regulatorio-e-contabil-das-psavs-prioridades-do-bcb) (Res. 548, 550, CMN 5.280, docs 5710/5711)
- Poder360 (BCB note) — [BC aprimora regras relativas à prestação de serviços de ativos virtuais](https://static.poder360.com.br/uploads/2026/09/BC-aprimora-regras-relativas-a-prestacao-de-servicos-de-ativos-virtuais.pdf)
- IstoÉ Dinheiro — [BC fixa prazo máximo de 3 anos](https://istoedinheiro.com.br/bc-fixa-prazo-maximo-de-3-anos-para-autorizar-prestadores-de-servicos-de-criptoativos-2)
- bpmoney — [BC dá até 3 anos para autorizar plataformas de criptoativos](https://bpmoney.com.br/brasil/bc-da-ate-3-anos-para-autorizar-plataformas-de-criptoativos/)
- okai — [Banco Central regulamenta o uso de ativos virtuais](https://okai.com.br/noticia/2025-11-10/banco-central-regulamenta-o-uso-de-ativos-virtuais-e-o-funcionamento-e-a-autorizacao-das-instituicoes-que-atuam-nesse-mercado)
- Jornal Contábil — [Receita Federal reforça cerco contra criptoativos](https://jornalcontabil.com.br/noticia/receita-federal-reforca-cerco-contra-criptoativo-para-deter-crime-organizado/) (DeCripto, CARF)
- Rota da Jurisprudência — [IN RFB 2.291](https://rotadajurisprudencia.com.br/?p=10856)
- The Regulatory Review — [Auditing Payment Stablecoins Under the GENIUS Act](https://www.theregreview.org/2025/11/17/krause-krause-auditing-payment-stablecoins-under-the-genius-act/)
- Eco — [Stablecoin issuer reserves explained](https://eco.com/support/en/articles/14796326-stablecoin-issuer-reserves-explained) (cadences, DeFiLlama as source)
- CoinDesk via NYDIG — [USD1 attestation lag](https://rss.boorghani.com/?p=353994)
- AWS — [AWS Public Blockchain Data (Marketplace listing)](https://aws.amazon.com/marketplace/pp/prodview-w2rxwj7aw6mt6); [Registry of Open Data](https://registry.opendata.aws/aws-public-blockchain); [re:Post AWS Open Data tag](https://repost.aws/tags/TApd0Wl5P8S9O6riTWmh-cGw/aws-open-data) (freshness reports)
- Caylent — [S3 Vectors pricing](https://caylent.com/blog/architecting-gen-ai-at-scale-lessons-from-aws-s-3-vector-store-and-the-nuances-of-hybrid-vector-storage); Classmethod — [S3 Vectors query cost cut](https://dev.classmethod.jp/en/articles/20260616-s3-vectors-query-charges/)
- Blockworks — [GovHub](https://blockworks.co/news/govhub-tracking-governance); Polkassembly — [AI governance layer proposal](https://polkadot-static.polkassembly.io/post/3389)
