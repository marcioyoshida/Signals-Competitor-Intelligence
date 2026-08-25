# ADR 012 — Entity-tagged corporate-distress store (Recuperação Judicial / falência)

- Status: **Accepted & implemented (v1)** — 2026-08-25. Owner-requested ("Go for A").
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
