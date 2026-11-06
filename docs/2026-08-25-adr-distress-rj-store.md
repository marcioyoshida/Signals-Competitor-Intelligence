# ADR 012 — Entity-tagged corporate-distress store (Recuperação Judicial / falência)

- Status: **Concluded (2026-10-28)**. The decision (option A) is built and live:
  `distress.py`, `feed.json.distress`, and Guard 4 against counterparty-bank framing
  (`d1d4638`, #73). Of the optional follow-ups, (1) and option C have shipped; option B and
  join (3) are unbuilt and unticketed. Both are optional engineering, see *Status / next
  steps*. Was: **Accepted & implemented (v1)** — 2026-08-25. Owner-requested ("Go for A").
- Delivers an **entity-level RJ/distress dataset** (the follow-up to the DataJud
  sector-distress signal, #25). Code: `src/synth/distress.py`,
  `tests/test_distress.py`, wired into `src/synth/lambda_handler.py` and surfaced
  in `src/dashboard/feed_builder.py` (`feed.json.distress`).
- Builds on: the entities registry resolver
  ([ADR 001](2026-08-17-adr-entities-registry.md)) `resolve_entities`, the news
  ingestion (`trade_press`), the deferred-news-commit path
  ([issue #23](../)), and the DataJud macro-distress source (`src/ingest/datajud.py`).

## Context — can we compile it from the current raw files?

Audited the live raw corpus to answer "could an updated dataset of entities under
Recuperação Judicial be compiled from current raw ingested files, or is another
ingestion needed?". Findings:

| Source | RJ signal | Entity-linked? | Persisted? |
|---|---|---|---|
| **DataJud** (`macro.distress`) | ✅ 65 RJ filings/run | ❌ public API is **party-name-scrubbed** | only aggregate counts (`items: []`) |
| **News** (`trade_press`) | ✅ "<empresa> pede RJ" — names the company | ✅ | ❌ **digest-only, overwritten each run** |
| raw **DOU** | grep = 0 | — | organ-filtered to BCB/CVM/CoAF; no judicial section |
| **CVM Fato Relevante** | 0 now | ✅ (listed issuer self-reports) | ✅ but episodic + listed-only |

Conclusion: **the current raw files cannot yield an entity-level RJ list** — the
entity-tied evidence is scrubbed (DataJud), filtered out (DOU), or thrown away each
run (news). A dataset needs *retention + tagging*, not necessarily a brand-new source.

## Decision — Option A: mine + persist the news stream

Classify RJ/falência in the **news slice we already ingest**, resolve it to registry
entities, and fold matched events into a **durable `distress/index.json`** keyed by
`(entity, kind)` with first/last-seen. This turns a one-shot headline into a
persisted status. **No new ingestion source** — the change is a classifier + a store.
DataJud stays the anonymized macro *trend* behind it.

Chosen over: (B) widening DOU to the judicial section, (C) CVM-FR tag only [listed
subset], (D) paid named-process DataJud tier. A is the cheapest with the broadest
coverage (any named company, not just listed); B/C/D remain future enrichers and can
layer on (option E: join DataJud's trend volume with A/B/C's names).

### Mechanics
- `classify_distress(title)` — accent-folded phrase match →
  `recuperacao_judicial | recuperacao_extrajudicial | falencia` (most-specific first).
- `detect_distress_events(news_items, resolver)` — a title must **both** classify as
  distress **and** resolve to a tracked entity via `resolve_entities` (anchored,
  ambiguity-gated) — a bare mention never creates a record. **Precision-first**, same
  discipline as the news-corroboration floor.
- `merge_distress(existing, events, ttl_days=720)` — upsert by `(entity, kind)`;
  keep `first_seen`/`last_seen`, latest title/url, evidence sample, mention count;
  prune records untouched for the TTL (a distressed status is durable — a company
  stays in RJ for years). Escalation to `falencia` is its own record; `entity_status`
  returns the most severe (falência > RJ > extrajudicial).
- **Run site:** the synth Lambda, right after the deferred news-commit — it scans the
  full fetched news (`news.items + news.context`, not just the fused candidates, so an
  RJ event that didn't clear the fusion floor is still recorded), then persists.
  Best-effort: a failure never breaks synthesis. No new IAM (synth already has
  digests-bucket read + put).
- **Surface:** `feed_builder` loads the index into `feed.json.distress` (read-only,
  most-recent first) — available to the dashboard and, via `feed.json`, to the
  grounded agent ([ADR 010](2026-08-25-adr-agent-chat-ui.md)).

## Guardrails
- Precision over recall — resolve-gated; bare/ambiguous mentions dropped (the
  registry `ambiguous_tokens` + source-attribution guards still apply through
  `resolve_entities`).
- **Defamation / accuracy** — a distress record is an *observed public event*
  (court filing reported in the press), tied to a public company in a public role;
  it carries the headline + source url as evidence. No inference beyond the reported
  filing; escalation states are explicit, not assumed.
- Durable but pruned — TTL keeps the store from growing unbounded; a status that
  stops being referenced for 2y drops out.

## Consequences
- The registry now accrues a **living RJ/distress dataset** for any named company
  in the news, at near-zero marginal cost (reuses news ingestion; one small S3 doc).
- Coverage is bounded by news recall (a company with no press coverage of its filing
  won't appear) — B/DOU and C/CVM-FR can be layered later for official confirmation.
- Verified live: "Banco Master entra com pedido de recuperação judicial" →
  resolves to `banco_master`, kind `recuperacao_judicial`; unrelated/unresolved
  headlines correctly dropped. `tests/test_distress.py` (10) green.

## Status / next steps
Implemented v1. Optional follow-ups: (1) a dashboard distress badge/panel on the
entity view; (2) option B (DOU judicial section) or C (CVM-FR tag) for official
confirmation; (3) join with DataJud's macro trend (option E) for a "sector distress
rising + here are the names" view.

**2026-10-28 audit:**
- (1) **SHIPPED.** There is a distress badge in the v1 "Radar de Risco" tab
  (`site/index.html`, `.distress-badge`). The v3 `/exec` board has an "Insolvência
  (confirmada)" tile and a CCO `distress` lens, and distress is a push-alert type.
- (2) **C SHIPPED** through [ADR 013](2026-08-25-adr-entity-classification-attributes.md):
  `update_from_digest` reads the CVM Fato Relevante (`fatos`) stream as the `regulatory` trust
  tier, and ADR 013 adds the confidence tiers. **B (DOU judicial section) is NOT built.**
  `dou.py` has no judicial or RJ handling.
- (3) **NOT built.** No DataJud × distress join exists in `executive.py` or
  `feed_builder.py`. DataJud stays the macro trend only.
- B and (3) are optional engineering work with no owner gate and no open ticket.

## Addendum, 2026-11-06: beyond the tracked registry (milestone "Distress coverage", #354–#359)

**Trigger.** Onça missed the Grupo Euro17 recuperação judicial. Facts: TJSP, filed around
2026-10-16; R$ 1,72 bi across eight companies; NeoFeed exclusive on 2026-10-22. An audit on
2026-11-05 found the 2026-10-28 "Concluded" status hid four breaks:

1. **Fetched, then dropped.** NeoFeed is an outlet feed, but `fetch_news` keeps an outlet item
   only when it names a tracked term. The RJ headline never reached `detect_distress_events`.
2. **Tracked-only by design.** The group's BCB-authorised SCD (cnpj root 59855096) had sat in
   the review queue since 2026-09-03, blocked by the prominence gate. Even a fetched headline
   could not resolve.
3. **Inert surfaces.** `_trusted_distress` admitted only `confidence == "confirmed"`, which no
   producer writes, so every executive insolvency tile read 0. Feed rows had no `date`, so
   distress push alerts never fired.
4. **The store never held a true record.** Every record it ever had was a false positive,
   later purged (#33, #55, #73, #296). On 2026-11-05 it held 0 records.

**Decisions.**
- **#354 Sweep.** Every fetched outlet headline, plus four distress topic queries, is
  classified. Distress headlines ride the news slice as `distress_items`: compact, capped at 60,
  outside the seen-set. The classifier adds `liquidacao_extrajudicial`, `intervencao` and `raet`.
- **#355 Wide resolution** (`src/synth/distress_watch.py`). A headline that passes guards 1–5
  but names no tracked entity is matched against three sources: the BCB register snapshot, the
  pending `discovery` reviews, and tracked entities with `news_search=False`. Precision rules:
  - the name must be the clause subject (before the phrase, or its *da/do/de* object after it);
  - a legal-name match means the institution itself (`role: self`);
  - a brand match requires a distinctive head (at least 5 characters or alphanumeric, not
    generic) owned by at most 3 CNPJ roots. It yields `role: group` unless a segment cue
    follows the brand ("Euro17 SCD").

  Results go to an `untracked` map keyed by CNPJ root. A matched structured candidate is
  **promoted**: distress overrides the prominence gate, the review is approved with
  `decided_by: distress_override`, and the record moves onto the new entity.
- **#356 Group linkage.** A group record is keyed `<entity>#group:<kind>` and labelled "Grupo
  econômico em …". It never merges into, or outranks, the entity's own record. Tracked entities
  that share a PF sócio (same name and masked doc in `graph/watchlist_qsa.json`) get a group row
  too.
- **#357 Correspondents.** Each distinctive brand gets one lookup in BCB
  `Informes_Correspondentes`, once per record. Contracting tracked banks get a `correspondent`
  row (tenant-scoped to the bank) and a CCO risk-register line (`terceiro`). Trusted confidence
  only.
- **#358 Register exits.** The ingest keeps `reference/bcb_institutions.json` and diffs it run
  over run. Exits go to `distress/registry_exits.json`. A resource that shrank by more than 5% is
  treated as a partial fetch and skipped. Synth records exits as `saida_cadastro_bcb` with
  `regulatory` confidence; this is a fact about the listing, not a cause.
- **#359 Review queue.** Approving a `discovery` review used to create nothing, because
  `_apply_review` had no branch for it; that is fixed. Producers append `signals`, and the
  queue, the admin API and the feed read in signal-score order, with `stale` for signal-less
  items older than 60 days. The ranking is computed at read time; no live data is rewritten.

**Guardrail unchanged.** Board and executive surfaces state only own-entity distress at
regulatory, curated or corroborated confidence. Group and correspondent rows are third-party
risk, never "the entity is insolvent".

**Verified offline.** In a live dry run on 2026-11-06 the sweep kept 25 headlines out of 643.
Only "Grupo Euro17 pede recuperação judicial" matched a BCB institution (EURO17 SCD, `group`).
Non-financial RJs (Aeris, Chilli Beans, Kitchens) matched nothing. The correspondent lookup
returned Votorantim and Daycoval. `tests/test_distress_watch.py` has 20 tests.

**First live run, 2026-11-06 03:11 UTC: two false positives, caught and purged before any feed rebuild.**
- "Aeris Energy recorre a recuperação extrajudicial" was bound to a tracked FII (Energy Resort) through the word "energy".
- "Fábrica de pás eólicas do Nordeste pede RJ" was bound to a Receita-CNAE review candidate (Nordeste Saúde Empresarial). That candidate was auto-approved, but no entity was created because its profile had no `entity_id`.
- Both fanned out to unrelated "ENERGY …" / "NORDESTE …" correspondents.

Reverted (backups under `backups/distress/` and `backups/registry/`), then four rules were added:
1. A brand head binds only at the start of the distress clause, after "Grupo", or as the first word of the genitive object.
2. A head carried by more than 5 distinct companies in the BCB correspondent register is an ordinary word, so the match is attributed to no one. With no way to vet a brand, no group claim is made.
3. Only a regulated-register profile (BCB/SUSEP/SPA, with `entity_id` and a licence class) is auto-promoted.
4. Fund vehicles and non-regulated review candidates match by full legal name only.

The re-run in a live dry run returns exactly one event: Euro17, as a group, with correspondents Daycoval and Votorantim.

### Addendum 2026-11-06 — official resolution regimes and the lifecycle gate (#361–#366)

**Trigger: the Will Bank miss.** BCB put Will Financeira (Will Bank) into liquidação extrajudicial by Ato do Presidente nº 1376 on 2026-01-21. Banco Master, its controller, went in by Ato nº 1369 on 2025-11-18. Both banks were tracked (curated since 08-30), yet the store held nothing for them, because:
- the decrees predate the store and the 30-day sweep;
- the later news names the standing status, not the event ("Liquidante do Banco Master…", "credores do Will Bank");
- neither entity had a CNPJ.

Worse, the feed showed both as growing competitors:
- an Ansoff "Will Bank expanding by integrating Mastercard" at 0.9 (the source was Mastercard settling its card bills in liquidation);
- SWOT opportunities;
- a "Lançamento de produto" behavioral front;
- an industry-inferred "Autorização BCB" badge.

- **#362 Official source.** BCB "Regimes de resolução" (`bcb.gov.br/estabilidadefinanceira/resolucao`; `/api/` is allowed by robots.txt).
  - `regime_resolucao_ativo?filtro=(Situacao eq 'Ativo')` lists the 25 active regimes. `Regesp_Internet/Detalhamento?identificador=` gives CNPJ, type, decree date, act, liquidator and status.
  - `src/ingest/bcb_regimes.py` keeps `reference/bcb_regimes.json` (structured ingest). A regime that leaves the list is re-read once for its closure reason; a conversion into falência is a `falencia` event.
  - The synth watch emits `regulatory` events dated at the decree and **re-observed** every run while active (`observed` → `last_seen`), so a decades-old liquidation such as Banco Rural (2013) never expires by TTL.
  - **History** uses the BCB's own closed-status lists (1,146 regimes across 19 closed statuses, incl. 375 "Encerrado (Falência.)"), refreshed weekly.
    - Details are fetched only for names equal to a tracked entity's.
    - A detail binds only by the entity's own CNPJ, within 5 years.
    - An Internet Archive fallback was not needed.
  - Correspondent fan-out for a regime requires the regime's exact CNPJ.
- **#361 Register exits are not distress.** The first exits (3 cooperatives) were mergers, e.g. Acredicoop → Viacredi. Exits stay in `distress/registry_exits.json`; a regime-caused exit reaches the store as its regime.
- **#363 CNPJ backfill.** For tracked entities with no CNPJ, a root is attached on one of two exact matches:
  - the legal name minus its legal-form tail equals an alias ("BANCO MASTER S/A" ↔ Banco Master);
  - the institution's own e-mail or site domain equals the alias without spaces ("willbank.com.br" ↔ Will Bank), counted only when no other regime shares the domain. The Master group shares its liquidator's `bancomaster.com.br` across 5 regimes.

  A legal-name match outranks a domain match, `add_cnpj_roots` never steals a root, and an ambiguous match binds nothing.
- **#366 Controllers.** An entity's official own distress (regulatory or curated source) adds a `group` row to every tracked entity listing it in `controllers` (Will Bank ← Banco Master).
- **#365 Status mentions.** "liquidante/interventor do X", "massa liquidanda/falida", "em liquidação", "credores do X" + FGC/liquid…, "quebra/colapso do X", "quebrou/faliu". These pass the same guards but only refresh an EXISTING own trusted liquidação, intervenção, RAET or falência record (`status_mentions`, `last_status_*`). They never create one or add a confidence source.
- **#364 Lifecycle gate** (`src/synth/lifecycle.py`, at feed build).
  - Applies to an entity's own trusted, still-active liquidação, falência or intervenção.
  - Sets `entity_attrs[e].lifecycle`.
  - Withholds SWOT S/O and its proposals, TOWS, Ansoff, BCG, Four Corners and behavioral cards.
  - Drops the industry-inferred BCB badge and the CSO posture row.
  - RAET is labelled but not gated, since operations continue.

**Verified in a read-only live dry run on 2026-11-06, before any write.** Every match was read:
- 25 active regimes;
- tracked: Banco Master (legal name), Will Bank (own domain), MW IP (existing CNPJ);
- Will Bank's group row via Banco Master;
- 22 untracked official liquidations;
- one correspondent link (Portocred → PAN, exact CNPJ);
- one history name match (a 1987 DTVM), correctly not bound.
