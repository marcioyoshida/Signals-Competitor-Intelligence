# Story — Operatives / person-graph ingestion (+ added-coverage eligibility)

Status: **P1 SHIPPED (2026-08-23)**; P2–P4 proposed. Builds on
[ADR 003](2026-08-19-adr-narrative-dimensions.md) Wave 3 (the operatives axis) and the
ingestion reference [DATA_SOURCES.md](DATA_SOURCES.md).

> **P1 built (2026-08-23).** `src/ingest/watchlist_qsa.py` (`OncaWatchlistQsa` Lambda,
> wired `… → relational → watchlist_qsa → operatives → …`) fetches each tracked entity's
> QSA (BrasilAPI, by the registry's CNPJ roots) and writes `graph/watchlist_qsa.json`
> — TTL-gated (30d) + bounded (10 lookups/run, so a cold start spreads gently). Only PF
> sócios (`identificador_de_socio == 2`), control-role-first, capped per entity. It emits
> the **masked** CPF only (`***XXXXXX**`) — `_safe_mask` re-masks defensively so a full
> CPF can never be persisted. `operatives` now reads that slice and keys persons by
> **(name, masked-CPF)**, which (a) separates homonyms — two different "João Silva" no
> longer merge into a false common-control edge — and (b) grounds a genuine **control
> cohort** (same name + same masked CPF across ≥2 entities) as a resolved fact, not a
> guess. Full CPF is never fetched, reconstructed (6 of 11 digits), or stored; everything
> stays review-gated. This takes operatives out of `source_gated` for the real watchlist.
>
> The review queue is **gated to control-cohort-relevant people** — a control-role sócio
> or someone bridging ≥2 entities — so a big bank's whole statutory board doesn't flood
> it; the full persons graph still resolves everyone (for cohort math). `_merge` is now
> **self-healing**: a pending proposal the tightened gate no longer produces is dropped,
> while human-decided ones persist.
>
> **Live (2026-08-23):** operatives is now `ok`, not `source_gated`. Coverage finding —
> only **3** tracked entities (cielo/getnet/rede) currently carry a **CNPJ root** in the
> registry, so QSA fetched for those 3 (41 sócios; masked docs verified, no full CPF).
> They share no individual controller (subsidiaries of different banks) → 0 common-control,
> honestly. The gated queue holds **1** control-role person. **Follow-up:** populate
> `cnpj_roots` on the curated entities (a registry-curation task) to widen QSA coverage —
> that is the real lever on person-graph reach, and where cross-entity control cohorts
> will actually surface (shared sócios among smaller fintechs).
>
> **CNPJ-root curation done (2026-08-23).** `src/synth/entity_registry.add_cnpj_roots`
> (non-destructive, CNPJ# reindex, never steals a root) + a **verified** curation tool
> `src/synth/cnpj_curation.py` (seed map → fetch BrasilAPI → the entity's own registry
> aliases must appear in the razão social → only then write; `python -m
> src.synth.cnpj_curation [--apply]`). The verification gate earned its keep: of 25
> seeded roots it **rejected 2 wrong ones** (`creditas` resolved to a person, IALDO
> MARQUES FALCÃO; `caixa_seguridade` to ALMEIDA PRODUÇÕES) — never written — and wrote
> **22** verified. Registry `cnpj_roots` coverage **3 → 25**; QSA then fetched 306 sócios;
> operatives resolved **297 persons**, queued **37** control-relevant, **0 false
> common-control**, **0 full CPFs**. Masked-CPF cohorting confirmed genuine same-person
> bridges (e.g. shared *directors* across Itaú and its acquirer Rede, same `***225618**`).
> Remaining: correct CNPJs for creditas/caixa_seguridade, and roots for the other
> Brazilian FS entities lacking them (betting/crypto/FII tickers intentionally skipped).

## The story

> As the analyst, I want the **person layer** of the relationship graph populated —
> the people who control, direct, or litigate over the tracked competitors — so that
> `OncaOperatives` can surface **person nodes** and **common-control edges** (one
> person bridging two competitors) for review, instead of sitting `source_gated`.

**Why it's blocked, exactly.** The synthesis side is done: `src/synth/operatives.py`
already resolves person nodes and `common_control` edges from person-bearing fields
(`PERSON_ROLE_FIELDS` = controllers/socios/directors/board/respondents/parties/counsel),
under the LGPD guardrails (name+role+document required, no CPF, public professional
roles only, institutional names excluded, everything review-gated). It is built, unit-
tested, wired into the pipeline — and **`source_gated` live**, because ingestion emits
no person names for the tracked watchlist:

- `src/ingest/receita_cnpj.py` **does** extract the QSA (`qsa[].{nome_socio,
  qualificacao_socio}` → `controllers` + a `partners` list), **but** `enrich_entrants`
  runs it **only over new BCB entrants** (`lambda_port.py:450`). The tracked competitors
  are established institutions, never "new entrants", so their QSA is never fetched.
- `src/ingest/dou.py` fetches DOU acts but does **not** parse named parties/signatories.
- There is no board/officer source (CVM Formulário de Referência) and no ownership
  source (B3 shareholding).

So the person layer is a pure **ingestion gap**, not a synthesis gap. This story fills
it. Each source below is mapped to the `operatives` field it feeds, so the axis
self-activates the moment the data lands (input-gating discipline — no synth change).

## Person-graph enablers (the work of this story)

Ordered by leverage — Phase 1 alone lights up the axis for the real watchlist.

| # | Source | Feeds (`operatives` field) | Effort | Notes |
|---|---|---|---|---|
| **P1** | **Watchlist QSA enrichment** (Receita) | `socios`, `controllers` | **Low** | Reuse `receita_cnpj.fetch_cnpj` — call it for the **tracked entities' CNPJ roots** (from the registry), not just entrants. Emit a per-entity `qsa` slice into the digest. This is the unlock: person nodes + common_control across the actual competitors. |
| **P2** | **DOU party extraction** | `parties`, `respondents`, `counsel` | Med | Parse `dou.py` acts for named parties/advogados (public administrative/judicial roles). Litigation/enforcement person links. |
| **P3** | **CVM Formulário de Referência (FRE)** | `directors`, `board` | Med | Administradores / conselho / diretoria of **listed** competitors — the board/officer layer. Part of the CVM-Demonstrações gap below. |
| **P3** | **B3 posições acionárias / shareholding** | `controllers` (with %) | Med–High | Hard ownership positions → high-confidence controllers and common-control edges backed by shareholding, not just QSA. |
| **P4** | **CVM intermediary registries** | advisors/auditors/coordinators | Low–Med | Already flagged in DATA_SOURCES as "relationship-graph enrichment" (named PLDFT/compliance officers, offering coordinators, auditors). Relational, not standalone. |

**Acceptance criteria**
- A tracked competitor with public QSA yields ≥1 **person-node proposal** (pending),
  name+role+document present, no CPF stored or keyed.
- A person bridging ≥2 tracked competitors in a control role yields a **common_control
  edge proposal** (pending), with both entities named.
- Institutional names (DTVM / asset / S.A.) are excluded; admin/manager/leader stay
  institutional. All outputs land in `graph/person_proposals.json`, review-gated.
- `operatives` reports `persons > 0` (leaves `source_gated`); no synth change required.
- LGPD guardrails from `operatives.py` are reused verbatim — this story adds **inputs**,
  not new guardrails.

**Phasing.** P1 (watchlist QSA) is the minimum to leave `source_gated` and is low-effort
(reuses an existing fetcher + the registry's CNPJ roots). P2–P4 deepen coverage and can
land independently; each self-activates its `operatives` field on arrival.

---

## Added-coverage eligibility check (the proposed source list)

Evaluated against what the repo already ingests (see [DATA_SOURCES.md](DATA_SOURCES.md)).
"Covered" = a wired Lambda ingester exists; "Gap" = no module; **bold** = recommended
to add.

| Proposed source | Status today | Person-graph? | Verdict |
|---|---|---|---|
| **News / Imprensa (Google News)** | **Covered** — `trade_press.py` (wired, news slice) | no | Not new work. *Formalize the folder/naming in DATA_SOURCES; no ingester to build.* |
| **BCB-IFData** | **Covered** — `bcb_ifdata.py` (wired, snapshot/ranking) | no | Optional **enhancement**: per-competitor time-series moves (today it's a ranking snapshot, not `detect_moves`). Low priority. |
| **BCB-Series / SGS** | **Covered** — `bcb_macro.py` (Selic SGS-432, Focus: IPCA/Selic/PIB/Câmbio) | no | Mostly covered. **Gap within it:** SCR credit volumes + inadimplência (Tier C, not implemented) — optional macro/credit add. |
| **CVM-Demonstrações (DFP/ITR + FRE)** | **Gap** — no module | **FRE yes** (board/officers) | **Eligible.** DFP/ITR = listed-competitor financials; **FRE = P3 person-graph enabler**. Recommend adding FRE with this story, DFP/ITR as a separate financials story. |
| **B3 (shareholding, corporate actions)** | **Gap** — no module | **yes** (ownership) | **Eligible + person-graph-relevant.** Shareholding = P3 here; corporate actions = a separate market story. |
| **SUSEP (insurance)** | **Gap** — no module | no | **Eligible, high-value** — Bradesco Seguros, **Caixa Seguridade, BB Seguridade are in the tracked set** and their insurance arm is invisible today. Recommend a **separate coverage story**. |
| **Entity-Master / Reference-Data** | **Covered** — DynamoDB entities registry is the single source of truth ([ADR](2026-08-17-adr-entities-registry.md)) | supports resolution | Largely done. Minor gap: conglomerate grouping + historical name changes (partly via the registry's `canonical_id` / review queue). |
| **Processed / layered structure** | **Partial** — raw digests (`lambda-digests/`) + `narratives/` + `features/` + `swot/` + `graph/` + `threads/` already layer raw→derived | — | Architectural, not a source. The enriched/clusters layers are effectively the derived stores. Optional future refactor; **not urgent**, don't block this story on it. |

### Recommendation — what to add where

- **Into this story (person-graph):** P1 watchlist QSA · P2 DOU parties · **P3 CVM FRE**
  · **P3 B3 shareholding** · P4 CVM intermediary registries.
- **Separate coverage stories (general value, not person-graph):**
  - **SUSEP** — insurance arms of tracked banks (high value; own story).
  - **CVM DFP/ITR** — listed-competitor financial statements (deeper benchmarking).
  - **BCB SCR / inadimplência** — credit-volume + default macro (Tier C completion).
- **Already covered / no new ingester:** News, IF.data (snapshot), BCB-Series (Selic/
  IPCA/Focus/FX), Entity-Master.
- **Deferred / tracked elsewhere:** the layered `Processed/` refactor (architectural);
  CVM FII/FIAGRO informe-mensal (already planned —
  [fii-structured-source-plan](2026-08-20-fii-structured-source-plan.md)).

## Guardrails & fit (unchanged from ADR 003)

Person data is the highest-LGPD-risk surface in the product. This story adds **only
public professional-role data** (QSA, board, DOU parties, shareholding) and routes
everything through the existing `operatives` guardrails: name+role+document required,
**no CPF** stored or used as a key, institutional names excluded, and **every** person
node / common-control edge is a **pending, review-gated proposal** — never auto-
published. Ingestion keeps the source URL for the citation trail. Nothing here changes
the synthesis or the vetting path; it supplies inputs the built machinery already knows
how to consume.
