# ADR 023 — International Banking-Regulatory Ingestion (Basel / BCBS / BIS)

Status: **PROPOSED** 2026-09-06. **Pilot-validated** (feasibility probed live; a working
ingester runs — see [`scripts/pilot_bis_bcbs.py`](../scripts/pilot_bis_bcbs.py)).

Relates to / extends: the **regulatory-change-intelligence** line
([ADR 2026-08-25](2026-08-25-adr-regulatory-change-intelligence.md) — `reg_change` /
`reg_documents` / `reg_diff` / `reg_change_record` / `reg_coverage`, all DOMESTIC BCB/DOU today),
[ADR 022](2026-09-06-adr-financial-soundness-prudential-ingestion.md) (the Basileia *numbers*;
this ADR is the *rules* behind them), ADR 019 (declarative source registry — a new source is a
`SourceSpec` + one `FETCHERS` entry), and the [[onca-reg-change-and-coverage]] arc.

## Context

Onça's regulatory-change machinery is complete but **domestic-only**: it fetches BCB normative
acts (`bcb_normativos.py`) and DOU (`dou.py`), enumerates what an amending act does (`reg_change`),
versions and diffs the text (`reg_documents` / `reg_diff`), and maps coverage (`reg_coverage`).
Nothing ingests the **international standards upstream of those acts** — the **Basel Committee
(BCBS)** framework and its updates, published at the **BIS**. Verified: no `basel`/`bcbs`/`bis`
source exists (the only in-repo "basel" strings are `baseline`).

This is the gap the request names: *ingest Basileia and other international banking-regulatory
updates*. It matters because the Basel framework is the **upstream cause** of the domestic rules
and of the Basileia ratios ADR 022 ingests — a BCBS standard (e.g. a capital or liquidity revision)
is what a later **Resolução CMN / Resolução BCB** transposes into Brazilian law. Monitoring only the
domestic act means we see the change *after* it lands; monitoring BCBS means we see it **coming**,
and can link the eventual domestic act back to its origin. It also answers officer questions Onça
currently cannot ("what's changing in Basel capital rules and when does it hit BR banks?").

## Feasibility (probed live, 2026-09-06)

- **BIS blocks bot UAs (403)** and has moved its publication URLs; it offers **no dedicated
  BCBS-publications RSS**. The pragmatic, working monitor is two `/doclist/*.rss` feeds, both 200
  with a **browser-like UA**, in **RSS 1.0 / RDF** (use `dc:date`, not `pubDate`), a rolling ~10–20
  item window (→ the seen-set / content-hash pattern handles recurrence):
  - **`/doclist/all_pressrels.rss`** — BIS press releases, carrying "Basel Committee …" announcements.
  - **`/doclist/bis_fsi_publs.rss`** — FSI Insights (Financial Stability Institute), incl. Basel
    *implementation* analysis.
- **Pilot result:** the ingester pulled real, dated items — 2 Basel Committee announcements (ICT-risk
  report) + FSI Insights (small-bank regulation, supervisory-LLM screening) — and normalized them
  into the reg-doc shape. **Relevance gate — found loose, then tightened:** the first pass admitted
  unrelated FSI/BIS working papers on generic terms (`liquidity`, `reserve`); a two-part gate (a
  STRONG trigger admits alone; WEAK prudential terms admit only with a banking-regulation CONTEXT
  word, and bare `liquidity` is excluded since it catches monetary-ops papers — prudential liquidity
  is caught by `LCR`/`NSFR`) dropped both clear false positives. What remains is BCBS + prudential +
  supervisory-tech (the last is borderline-but-adjacent). Keyword gating alone won't be perfect →
  **ship shadow-first**, and consider an LLM relevance pass (the existing synth) in production.

## Decision

Add an **international / upstream regulatory tier** to the existing reg-change machinery — a new
source, not a new pipeline.

### 1. `src/ingest/bis_bcbs.py` — the ingester (mirrors `bcb_normativos.py`)

- Fetch the two confirmed feeds with a browser UA; parse RSS 1.0/RDF (`title`, `link`, `dc:date`,
  `description`). Gate to **banking-regulation relevance** with a tightened, auditable trigger set
  (require Basel/BCBS/prudential/supervisory/capital-framework terms; drop bare `liquidity`/`reserve`).
- Normalize to the reg-doc shape already consumed downstream, tagged `tier="international"`,
  `source="BIS/BCBS"`, `kind="intl_reg_update"`, `lens="regulatory"`, with `id = "bis:"+sha1(url)`.
- Seen-set / content-hash gated (same discipline as `reg_documents`) so a re-fetch of the rolling
  window is a no-op; persist to a durable `intl_reg/index.json` store.

### 2. Feed the existing reg-change surfaces — don't build new ones

- The normalized records flow into `reg_coverage` (a new **international** row) and surface on the
  **CRO** board as an upstream regulatory signal, distinct from domestic acts.
- **Transposition link (the valuable join):** when a domestic `reg_change` act cites or matches a
  BCBS standard (by number/topic), link them so a Brazilian resolution shows its Basel origin, and a
  BCBS update shows "not yet transposed in BR" vs "transposed by Resolução X". This is the
  international↔domestic bridge that makes the tier more than a news feed.

### 3. Boundary with ADR 022 (numbers) and the synth (narrative)

- ADR 022 ingests the Basileia **ratio per bank** (IF.data). ADR 023 ingests the Basileia **rule
  updates** (BCBS). Together: "XP's Basileia is eroding (−2.01pp, ADR 022) *and* the capital
  framework is tightening (BCBS, ADR 023)" — number + cause.
- Deeper reading of a standard's PDF stays on the **existing Bedrock synth + KB** path (grounded,
  cited), exactly as ADR 022 §3 handles Pilar 3 — no new inference stage. English source is fine;
  the KB is language-agnostic.

## Consequences

**Pros.**
- Closes a real upstream gap with a working, ADR-019-shaped source feeding machinery that already
  exists; low marginal cost.
- **Leading-indicator on rules** to match ADR 022's leading-indicator on numbers; the transposition
  link is a genuinely differentiated signal for a BR regulated-banking audience.

**Cons / risks.**
- **BIS anti-bot + URL churn** — needs a browser UA and will break when BIS reshuffles again;
  contained by the retry/seen-set discipline and by treating the feed as best-effort.
- **No official BCBS-publications feed** — the press feed is a *proxy* for "what's new"; a
  standard published without a press release could be missed. Mitigation: also poll the FSI feed and
  (later) the BCBS publications HTML list behind the browser fetch.
- **Relevance-gate precision** — the pilot proved recall but showed false positives; the tightened
  trigger set must be validated before surfacing (ship the tier **shadow-first**, like ADR 022 §3).
- **Scope creep** — FSB / IOSCO / IAIS / IFRS are adjacent international bodies (systemic / securities
  / insurance / accounting). Out of scope here; BCBS-first, others only if a buyer needs them.

**Rejected alternatives.**
- *Scrape the BCBS publications HTML list directly now.* It 403s our UA and has no stable structure;
  the RSS feeds are the lower-friction start. Revisit as a Phase-2 backfill.
- *A separate international pipeline.* Unnecessary — this is a low-volume source that fits the daily
  ingest fan-out; no cadence argument like ADR 022 §4's monthly balancete.

## Phasing

1. `bis_bcbs.py` off the two confirmed RSS feeds + tightened relevance gate → `intl_reg/index.json`;
   shadow-first (stored, not surfaced) until gate precision is validated.
2. Surface on the CRO board + a new `reg_coverage` international row.
3. **Transposition link** BCBS standard ↔ domestic `reg_change` act (number/topic match).
4. Standard-PDF deep read via the existing synth + KB (grounded, cited), as needed.
5. *(Optional, buyer-driven)* extend to FSB / IOSCO / IAIS / IFRS.
