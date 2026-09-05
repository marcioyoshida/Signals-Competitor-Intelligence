# Product Officer (CPO) — ingestion requirements & the per-sector intelligence model

- Status: **REQUIREMENTS (raised 2026-09-05)** — accompanies the rich per-sector CPO build
  (`src/synth/executive.py` `build_cpo`/`_sector_profile`, v3 `/exec` CPO board). Extends
  [ADR 021](2026-09-04-adr-executive-flow-officer-dashboards.md) §G (CPO) and the coverage-gap
  loop ([ADR 014](2026-08-25-adr-coverage-gap-loop.md)).
- Owner ask: the CPO board "was not differentiating across industries — we're running thin on
  ingestion/narrative fields for Product." This doc (a) documents the **per-sector Product
  intelligence model** now built from existing data, and (b) **raises concrete ingestion
  requirements** for the fields that are genuinely thin, each tied to the CPO decision it unblocks.

## What the CPO now computes (from existing data — no new ingestion)

`build_cpo` derives a deep, per-sector **Product profile** (`feed.executive.cpo.portfolio[]`),
so the board differentiates hard across sectors today:

- **Coverage-maturity index** (0–100, transparent inference): `0.30·breadth(tracked/20) +
  0.30·depth(narratives/120) + 0.20·freshness + 0.20·provenance` — e.g. Banking 86, Fintech 79,
  Betting 68, agri-funds 59.
- **Provenance strength** per sector: the `entity_attrs.radar.tier` mix (official/structured/
  registry/identified) → a 0–100 score. Banking is registry+official (58); Betting/Insurance are
  all-official (100); agri-funds is identified-heavy (28).
- **Competitive structure**: distinct entities, **concentration** (top-3 share of the sector's
  cards — Banking 0.25 fragmented vs Insurance 0.64 concentrated), top-entities-by-signal.
- **Velocity & freshness**: `narratives_latest`, days-since-last-signal, alert rate.
- **Source diversity**: distinct lenses feeding the sector.
- **Discovery pipeline**: review-gated proposals per sector (Fintech 39, agri-funds 11).
- **Metadata completeness** (the exposé): % of the base carrying each Product field.

## The gap the exposé makes concrete

Base-wide Product-field completeness (2026-09-05): **ownership 100% · parent 15% · ticker 8% ·
esg 1% · certifications 0%**. Ownership is solved; the rest — especially **ESG, certifications,
and any market-size/share/pricing** — are the thin fields starving the Product lens. These are
the requirements below.

## Requirements (prioritized) — each tied to a CPO decision

| # | Field / dataset | CPO decision it unblocks | Source(s) | Cadence | Target | Notes |
|---|---|---|---|---|---|---|
| R1 | **ESG ratings/scores** per entity | "Which competitors lead/lag on ESG per sector?" | B3 ISE constituents (have), CVM ESG disclosures, MSCI/S&P where licensable | quarterly | ≥60% of tracked | issue **#30** open; extend `entity_attrs.esg` beyond the 7 ISE members; classify from `fatos`/news as a fallback (labelled inference) |
| R2 | **Certifications / licenses** per entity | "Is a competitor authorized/certified for segment X?" | BCB/CVM/SUSEP/PREVIC authorization registers (already partly ingested for discovery) | monthly | ≥50% of tracked | populate `entity_attrs.certifications` from the same BCB/CVM class data discovery already reads |
| R3 | **Market size & share** per sector | "How big is each sector; who leads by share?" JTBD/market-fit | BCB IF.data / CVM aggregates (AUM, credit stock), ANBIMA where licensable | quarterly | all covered sectors | new `feed.market_structure[sector]` = {size, top-N share}; feeds a real BCG-style share axis |
| R4 | **Pricing / tariff signals** per sector | "Where is price pressure moving?" | existing `juros`/`ofertas` lenses (have) → **structure per sector**; ANEEL/SUSEP tariffs for sectorial | daily/weekly | banking/adquirência/seguros first | promote the existing rate/fee narratives into a structured per-sector price index |
| R5 | **Source-health metadata** per source | "Is our data for sector X fresh/reliable?" | internal — instrument each ingester | per run | all sources | emit `{source, last_ok, docs, staleness}` → a CPO data-quality panel; complements the maturity index |
| R6 | **Firmographics** (founded, size band) per entity | competitive depth / new-entrant threat | CNPJ/Receita (partly available), news | on discovery | ≥40% | `entity_attrs.firmographics`; deepens the discovery→onboarding decision |

## Discipline (unchanged)

- Grounded/cited; a derived field (e.g. ESG-from-news) is **labelled an inference**, never a fact.
- Every new field flows through the registry as `entity_attrs.<field>` with provenance
  (ADR-018) so precedence/rollback apply; the CPO exposé reads completeness straight from it.
- Review-gated where the source is fuzzy; structured/official sources auto-populate.
- The maturity/provenance formulas are transparent and surfaced as inferences on the board.

## Sequencing

1. **R2 certifications** — cheapest (reuse the BCB/CVM class data discovery already pulls) and
   lifts a 0% field fast.
2. **R1 ESG** — resume #30; ISE constituents + CVM disclosures → `entity_attrs.esg`.
3. **R5 source-health** — internal instrumentation; unlocks a data-quality panel.
4. **R3 market-share / R4 pricing** — the higher-value, higher-effort market-structure sources.
5. **R6 firmographics** — opportunistic on discovery.

Related: ADR 021 §G (CPO), ADR 014 (coverage-gap loop), ADR 013 (entity classification attrs —
where `esg`/`certifications`/`ownership` live), ADR 018 (provenance), issue #30 (ESG).
