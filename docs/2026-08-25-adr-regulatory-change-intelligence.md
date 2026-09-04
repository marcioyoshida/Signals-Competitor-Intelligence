# ADR 009 — Regulatory Change Intelligence: document versioning, diff, and impact tagging

- Status: **Partial — Phase A SHIPPED** (2026-09-02); design 2026-08-25. Owner-requested
  expansion of the Regulatory axis.
  - **Phase A (deterministic amending-act parser) LIVE** — `src/synth/reg_change.py`
    enumerates the discrete changes an act declares (`altera`/`revoga`/`dá nova
    redação`/`inclui`/…) + the article/dispositivo refs + the base instrument each
    targets, bound per-clause and quoted (sourced, no LLM/network). Attached to the
    reg-lifecycle + radar cards as `changes[]`/`n_changes` + a "Mudanças: …" narrative
    line (`regulatory.py`). Fixed a latent `_NUM` truncation (undotted "5304"→"530").
  - **Phase B (versioned regdocs/ full-text store) LIVE** — `src/ingest/reg_documents.py`
    fetches a tracked instrument's full text and persists it keyed by instrument +
    content-hash (`regdocs/<instrument_key>/<hash>.txt` + `regdocs/index.json`), content-
    hash-cached so a re-fetch is a no-op. Full-text source = the **in.gov.br DOU** page
    (BCB `exibenormativo` is a JS shell); `regulatory.regdoc_targets()` derives the DOU URL
    from the thread's citations, **number-scoped** to the instrument (a thread aggregates
    other acts' citations — same nexus discipline as #51). Gated `ONCA_REGDOCS`; live run
    stored 5 instruments' full text (5336 = 5,348 chars). This is the enabler the diff phase
    needs.
  - **§2 section diff LIVE** — `src/synth/reg_diff.py` segments each stored version by
    structural unit (preamble / Art. N / Anexo), aligns by key, marks added/removed/modified
    (compact ± diff for modified), `summarize_diff` → "modifica Art. 1; inclui Art. 9;
    revoga Art. 7". Wired into `reg_documents.store_document`: a new version over a prior one
    persists `regdocs/<key>/<old>__<new>.diff.json` + a summary on the version entry. Fires
    on any changed version (rare for discrete acts; the enabler is in place). The grounded
    diff the §3 LLM record will describe + rate.
  - **Deferred:** (3) the bounded **LLM change-record** with rated impact/blast_radius/
    difficulty (Phases A + §2 are the grounded floor it builds on: the change list + the
    section delta); (4) attach the diff to the reg-lifecycle card; (5) the dashboard
    "Mudança regulatória" panel + "Regulatório/Mudanças" filter.
- Extends [ADR 003](2026-08-19-adr-narrative-dimensions.md) — the **regulatory-lifecycle
  axis** (`src/synth/regulatory.py`), which today tracks *that* an instrument changed +
  its deadline + affected domain, and **explicitly deferred to v2**: "fetch/compare the
  actual documents" and "per-entity compliance / blast radius." This ADR is that v2.
- Builds on the entities registry ([ADR 001](2026-08-17-adr-entities-registry.md)) for
  the industry taxonomy, and the SWOT/framework belief store
  ([ADR 004](2026-08-22-adr-competitive-thesis-swot.md)) for the Threat feed.

## Context

Today the regulatory axis is **precision-first but shallow**: `bcb_normativos.fetch_recent`
pulls only the `subject` (assunto) of a normativo — not its full text — and
`regulatory.py` threads instruments by a stable key, extracts a deadline by regex, and
infers an affected *domain* from a keyword taxonomy. So a card says *"IN BCB 767 changed
the DICT Manual, effective <date>, affects Pagamentos/PIX"* — useful, but it does **not**
tell the analyst **what actually changed, how big the change is, or how hard it is to
comply**. The owner's example makes the target explicit:

> *"Atualização da versão 8.5 do Manual Operacional do DICT: a IN BCB 767 altera a
> entrada em vigor de alguns dispositivos da v8.5 … exige ajustes nos sistemas
> gerenciais e nos bancos de dados de clientes …"*

The intelligence we want per regulatory update: **fetch the document(s), determine the
delta, enumerate the discrete changes, and tag each with impact, blast radius, affected
industries, implementation difficulty, effective date, and action required** — cited,
with the inferred attributes labeled as inference (per the product's no-fabrication rule).

Two shapes of "change" exist and the design must handle both:

1. **Versioned documents** (Manual Operacional do DICT v8.5, Regulamento do Pix, a
   Manual/Anexo): there is a *previous version* and a *new version* — the change is a
   **document diff**.
2. **Amending acts** (IN BCB 767, a Resolução that "altera" a prior act): the act text
   itself **enumerates the deltas** ("altera a entrada em vigor de …", "revoga o art. X")
   — the change is parsed *from the act*, and optionally applied against the base
   instrument it amends.

## Decision

Add a **Regulatory Change Intelligence** stage that, only for instruments the existing
emit-on-change gate flags as new/changed, produces a structured **change record** per
instrument version. Reuse the existing axis, store, and dashboard machinery — do not fork.

### 1. Store the documents (versioned raw corpus)

- Extend the ingest to fetch the **full text** of a changed instrument (the
  `exibenormativo` body for BCB acts; the linked **PDF/HTML manual** for versioned docs
  like the DICT Manual). Model on `sec_filings.enrich_with_content` (metadata first,
  bodies only for the diffed set — bounded).
- Persist to the raw corpus (`ONCA_RAW_BUCKET`) keyed by **instrument + version + fetch
  date**: `regdocs/<instrument_key>/<version>.{txt,pdf,json}`. This gives us the *prior*
  version to diff against next time — the store is the enabler for everything else.
- A small **instrument→document index** (`regdocs/index.json`) maps the instrument thread
  (already computed by `regulatory.py`) to its stored versions + hashes, so a re-fetch
  with an unchanged content hash is a no-op (cost control).

### 2. Determine the delta

- **Versioned docs:** segment both versions by structure (artigo / seção / item —
  regex + heading detection), align sections, and compute a section-level diff (added /
  removed / modified). Fall back to a normalized text diff when structure is unclear.
- **Amending acts:** parse the act's own change verbs (`altera`, `revoga`, `inclui`,
  `dá nova redação`, `entra em vigor`) + the article references they target — the act is
  a machine-readable changelog. Where the amended base instrument is stored, resolve the
  references to concrete before/after text.

The diff is deterministic and **cited** (each change points to the article/section + the
source URL). It is the grounded floor; the LLM only *describes and rates* it (§3).

### 3. Enumerate + tag each change (LLM, labeled inference)

For each discrete change, an LLM (Bedrock, the synth grant) emits a **change record**
from the diff + instrument context — a strict, bounded JSON call (model on the framework
drafters), never free-form:

```json
{
  "instrument": "Manual Operacional do DICT",
  "version": "8.5",
  "amended_by": "IN BCB 767",
  "change": "Altera a entrada em vigor de dispositivos da v8.5 do Manual do DICT.",
  "articles": ["<seção/dispositivo refs>"],
  "effective_date": "2026-…",
  "affected_industries": ["banking", "fintech", "acquiring"],   // from INDUSTRIES taxonomy
  "affected_surfaces": ["sistemas gerenciais", "base de clientes", "core DICT"],
  "impact": "…what teams must do…",
  "blast_radius": { "score": 0-1, "band": "narrow|sector|market", "n_entities": N },
  "difficulty": { "score": 0-1, "band": "low|medium|high", "drivers": ["DB migration","prazo curto"] },
  "action_required": "…",
  "confidence": 0-1,
  "is_inference": true          // change text is sourced; impact/blast/difficulty are rated
}
```

- **affected_industries** tag from the existing `INDUSTRIES` taxonomy (ADR 001).
- **blast_radius** = f(affected domains × tracked entities in those industries × number of
  affected surfaces). `n_entities` is concrete (count from the registry); `band`/`score`
  is the rated read. This is the "who/how-many is hit" the ADR-003 axis deferred.
- **difficulty** = rated effort (DB/schema migration, integration surface, deadline
  tightness), with named `drivers` so it's auditable, not a bare number.
- **Guardrails (unchanged product discipline):** the *change* and *effective_date* are
  sourced (article + URL); **impact / blast_radius / difficulty are labeled inference**
  (`is_inference`, `mode="derived"`, `axis="regulatory"`). No claim without the document
  citation. Cost-bounded: only changed instruments, bodies capped, content-hash-cached.

### 4. Feed integration

- The change record attaches to the existing **regulatory-lifecycle thread** for the
  instrument (`reg_lifecycle/index.json`) — one thread per instrument, now carrying its
  version history + per-version change records. Keeps the SWOT-Threat feed (a hard/urgent
  change is a stronger `swot_hint` "T").
- Emit-on-change already gates re-fires; a change record is (re)computed only when the
  content hash changes.

### 5. Dashboard surface — recommendation

The change record is richer than a one-line card, so it needs its own **context**, not a
cramped chip. Recommendation (fits the existing card + collapsible-panel model, avoids a
whole new nav):

- Render the regulatory-lifecycle card with an **expandable "Mudança regulatória" panel**
  (like the framework `.fw-card` tabs): the change summary, **affected-industries chips**,
  a **blast-radius** indicator, a **difficulty heatmap** chip (reuse `_confHeat`'s ramp),
  the **effective date / countdown**, affected surfaces, and action required — each change
  row cited.
- Add a **top-level filter** "Regulatório / Mudanças" (a facet on the existing feed, like
  the lens filter) so an analyst can pull just regulatory changes and sort by blast radius
  or deadline. A separate full **tab/route** is *not* needed at v1 — the panel + filter
  reuse the current single-page model; revisit a dedicated tab only if volume grows.

## Consequences

- New cost surface (document fetch + diff + one bounded LLM call per changed instrument),
  bounded by emit-on-change + content-hash caching + body caps. Absorbed by the
  parallelized pipeline (the regulatory branch already runs concurrently — see the #10
  two-phase DAG).
- The document store (`regdocs/`) is the durable enabler — even before diffing ships, it
  lets us reconstruct any instrument's history and is reusable for CVM/SUSEP later.
- Scope discipline: **impact/blast/difficulty are rated inference, always cited** — this
  ADR must not become a source of confident-but-unsourced compliance advice.

## Status / next steps

Proposed. Implementation order when picked up: (1) full-text fetch + `regdocs/` versioned
store + index (the enabler); (2) deterministic diff — amending-act parser first (higher
signal, easier), then versioned-doc section diff; (3) the bounded LLM change-record with
taxonomy tags + labeled-inference attributes; (4) attach to the reg-lifecycle thread; (5)
the dashboard change panel + "Regulatório/Mudanças" filter. Related backlog:
`docs/2026-08-16-roadmap.md`.
