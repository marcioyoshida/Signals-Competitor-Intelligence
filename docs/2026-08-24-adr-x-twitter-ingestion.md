# ADR 008 — X/Twitter ingestion: a scored, noise-filtered social subpipeline with vision OCR

- Status: **Planned (design only)** — issue #19. This ADR specifies the subpipeline;
  no code ships with it. Of the same issue batch, #25 DataJud and #24 CoAF (via the
  DOU organ filter) shipped; #26 JUCESP is **backlogged** (the PRODESP JUCESP API
  is contract-gated — a *Fluxo de Contratação* + issued credentials, not openly
  public). X/Twitter is carved out because it needs its own trust/curation
  machinery and a vision step.
- Extends [ADR 003](2026-08-19-adr-narrative-dimensions.md) (axes/lenses),
  [ADR 001 entities registry](2026-08-17-adr-entities-registry.md) (the DynamoDB
  registry pattern the source scorecard reuses), and the ingest→diff→synth
  pipeline in CLAUDE.md.

## Context

Every other Onça source is an **authoritative register** (BCB/CVM/SEC/DOU/DataJud):
low noise, citable, stable ids. X/Twitter is the opposite — a firehose of mostly
noise where a *few* accounts (company IR, executives, sector journalists,
regulators, specialised analysts) post **early, material, citable** signals, often
as **images** (a screenshot of a results table, a chart, a printed note). Issue #19
asks for four things: (1) filter for context, (2) a **source scorecard**, (3) read
graphs/numbers embedded in pictures, (4) register the **datetime of postage**.

Two hard constraints shape the design:

- **Onça's moat is cited, publicly-sourced intelligence** (CLAUDE.md): every
  synthesized claim carries a source URL and estimates are labelled. A social post
  is citable (the post URL) but **low-trust** — so it must be *scored and gated*,
  never fused as fact on par with a CVM filing.
- **Access is paid/gated.** The X API v2 is commercial (Basic/Pro tiers); there is
  no free firehose. So the ingester must be **account-scoped** (pull a curated
  watchlist of high-value authors), not a keyword firehose — which also happens to
  be the right *noise* strategy.

The decisive realisation: the noise problem and the access problem have the **same
answer** — a **curated, scored set of source accounts**. That set is per-tenant
curation → it belongs in DynamoDB (the ADR 001 registry pattern), editable by API,
not hardcoded.

## Decision

A **separate subpipeline** `x_ingest` running as its own Step Functions branch
(like the trade-press branch — it scales with the account list, one API window per
account), feeding the existing digest under a new **`social`** slice / **`x` lens**.

### 1. Source scorecard — a new DynamoDB table `onca-social-sources`

Curated authors with a **trust score**; the ingester only pulls accounts on this
list (solves access + noise at once). Schema (single-table, per ADR 001):

| attr | example | role |
|---|---|---|
| `pk` | `XSRC#nubank_ir` | handle key |
| `handle` | `@nubank` | the account |
| `platform` | `x` | future-proofs for Bluesky/LinkedIn |
| `entity_ids` | `["nubank"]` | tracked entities this author speaks for/about |
| `author_type` | `ir` \| `exec` \| `journalist` \| `regulator` \| `analyst` | class prior |
| `trust` | `0.0–1.0` | **source score** (see §4) — gates ingestion & weights synth |
| `verified` | `true` | platform verification (a weak prior) |
| `active` | `true` | soft-delete |
| `added_by` / `reviewed_at` | curator provenance | ties into the step-5 review queue |

`get_source` / `put_source` / a review queue mirror `entity_registry`. Seeded from
code, table authoritative — a curator adds/removes/re-scores handles with no deploy.

### 2. Ingestion — account-scoped, datetime-anchored

- For each `active` source above the trust floor, pull the recent window via the
  X API v2 `users/:id/tweets` (env `ONCA_X_BEARER`, tier-bounded), newest-first.
- **Register the datetime of postage** (issue #19.4): store `posted_at` from the
  API `created_at` (UTC → BRT), and use it as the diff/sort key — *not* the ingest
  time — so "X is new since last run" and the card's timestamp reflect the post.
- Stable id `x:<tweet_id>` for `detect_new` (same diff engine, own state source).
- Best-effort, degrades to `[]` (matches every other source).

### 3. Vision OCR — pictures with text/graphs → raw content

Many material posts are **images** (a results table screenshot, a chart, a printed
"comunicado"). Step (issue #19.3):

- If a post has media, call an **LLM vision model** (Bedrock — a Nova/Claude
  multimodal model already reachable from the synth Lambda's Bedrock grant) with a
  **strict extraction prompt**: *transcribe visible text verbatim; read axis
  labels and data points from charts into a compact table; output numbers with
  units; if illegible, say so — never infer.*
- The transcription is appended to the post's raw text as `ocr_text` and written to
  the corpus, so the number/table becomes searchable, citable raw content.
- **Guardrail:** OCR output is labelled `is_ocr` / `is_inference` and never treated
  as an authoritative figure — it is a *reading* of a social image, gated by the
  source trust like everything else. Cost-bounded: cap images/run, only for
  sources above a higher `trust` bar.

### 4. Noise filter, scoring & curation

Three gates, cheapest first (drop before spending on vision/LLM):

1. **Source gate** — author must be on the scorecard with `trust ≥ ONCA_X_MIN_TRUST`.
   (Removes the entire anonymous firehose by construction.)
2. **Context gate** — the post must resolve to a tracked entity (reuse
   `entity_registry.resolve_entities` on text + `entity_ids` prior) *and* clear a
   finance-context check (reuse the trade-press `_looks_like_finance` filter),
   dropping personal/off-topic posts from otherwise-good authors.
3. **Materiality gate** — engagement/keyword heuristic + the source's `author_type`
   prior to skip banter (memes, replies) vs. signal (results, guidance, M&A, hires).

**Post score** = `trust` (source) × materiality × context-confidence, capped. This
feeds synth as a *low-ceiling* lens (like `silence`) so a social post can
**corroborate** or **surface early** but never headline above a filed fact.
Curation closes the loop: mis-scored authors are re-scored / retired through the
existing review queue; a source whose posts repeatedly fail the context gate decays
in `trust`.

### 5. Integration & guardrails

- New `social`/`x` digest slice → synth candidate lens (`x`), shown on cards like
  any lens; the citation is the **post URL** (always present → passes the citation
  guardrail). OCR-derived numbers are labelled inference.
- **LGPD / defamation** (per ADR 003/004 discipline): posts about *people* stay in
  the review-gated operative/relational path, never auto-published; only
  entity-about-entity material is fused. Public figures acting in a public role only.
- **No login-gated scraping** — API access only (respects CLAUDE.md and X ToS).

## Consequences

- Adds a paid dependency (X API tier) and a vision-inference cost — both bounded by
  the account list size and per-run caps; both env-gated off by default so the rest
  of the pipeline is unaffected until provisioned.
- The scorecard table is the unlock: it makes X tractable (curated authors) and is
  the reusable substrate for future social platforms.
- Until X API access is provisioned, this stays design-only; the DynamoDB scorecard
  and the account-scoped/vision/scoring shape are the durable decisions here.

## Status / next steps

Planned. Implementation order when picked up: (1) `onca-social-sources` table +
`get/put_source` + seed; (2) account-scoped ingester with `posted_at` diff; (3)
the three-gate filter reusing existing resolvers; (4) Bedrock vision OCR with the
strict prompt + `is_ocr` labelling; (5) the `x` lens + review-queue curation.
