"""Lambda-style prototype for the Phase 1.5 ingestion pipeline.

This keeps the fetch functions pure and exposes a single event handler that
can later be wired to EventBridge + Lambda with minimal changes.

Sources:
  - BCB normativos (regulatory) — detect_new
  - CVM funds RCVM 175 registry (competitor launches) — detect_new
  - BCB IF.data market share — snapshot (no id-diff)
  - BCB autorizações (new entrants) — detect_new, first-run seed suppressed
  - BCB Pix (traction moves) — detect_moves via DynamoDB value state
  - BCB juros médios (pricing moves) — detect_moves via DynamoDB value state
  - CVM ofertas de distribuição — detect_new, first-run seed suppressed
  - SEC EDGAR (US-listed fintechs) — detect_new, first-run seed suppressed
  - CVM Informe Diário (fund AUM moves) — detect_moves via DynamoDB value state
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from contextlib import contextmanager
from typing import Any

import boto3

from src.diff.engine import DynamoDbState, DynamoDbValueState, detect_moves, detect_new


class _SourceBudgetExceeded(Exception):
    """A single source ran past its wall-clock budget (or the ingest deadline)."""


@contextmanager
def _source_budget(label: str, deadline: float, per_source: int):
    """Bound one source's wall-clock time so a slow endpoint can't eat the run.

    Enforces both a per-source cap and the overall ingest deadline. If the
    deadline has already passed, the source is skipped. Uses SIGALRM (main
    thread only); elsewhere it degrades to a no-op and relies on the caller's
    deadline checks. The raised error is an Exception, so each source's existing
    ``except Exception`` handler catches it and falls back to an empty result.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 1:
        raise _SourceBudgetExceeded(f"{label} skipped (ingest deadline reached)")

    use_alarm = hasattr(signal, "SIGALRM") and (
        threading.current_thread() is threading.main_thread()
    )
    if not use_alarm:
        yield
        return

    secs = max(1, int(min(per_source, remaining)))

    def _fire(signum, frame):
        raise _SourceBudgetExceeded(f"{label} exceeded {secs}s budget")

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _fire)
    signal.alarm(secs)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _ingest_deadline(context: Any) -> float:
    """Monotonic time by which ingestion must stop starting new work.

    Derived from the Lambda's actual remaining time (minus a reserve for the
    S3 digest write + return), so it adapts to the configured timeout. Falls
    back to a fixed budget when no Lambda context is available (local/tests).
    """
    reserve = int(os.environ.get("ONCA_INGEST_RESERVE_SEC", "25"))
    fallback = int(os.environ.get("ONCA_INGEST_BUDGET_SEC", "780"))
    now = time.monotonic()
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        remaining_s = context.get_remaining_time_in_millis() / 1000.0
        return now + max(5.0, remaining_s - reserve)
    return now + fallback
from src.ingest import (
    bcb_autorizacoes,
    bcb_ifdata,
    bcb_juros,
    bcb_macro,
    bcb_normativos,
    bcb_pix,
    cvm_fiagro,
    cvm_fundos,
    cvm_inf_diario,
    cvm_ipe,
    cvm_ofertas,
    bcb_reclamacoes,
    cade,
    ceis_cnep,
    consumidor_gov,
    datajud,
    dou,
    pncp_contratos,
    raw_writer,
    registry,
    receita_cnpj,
    reclame_aqui,
    sec_filings,
    trade_press,
)


def _new_since_last_run(
    source: str,
    docs: list[dict[str, Any]],
    *,
    seed_if_empty: bool = False,
    commit: bool = True,
) -> list[dict[str, Any]]:
    """Diff docs against DynamoDB-backed state; degrade gracefully on failure.

    When seed_if_empty is True (autorizações registry), the first run with
    an empty state table seeds the baseline and reports nothing — otherwise
    every authorized institution would appear as a "new entrant".

    ``commit=False`` computes the fresh set without marking anything seen; the
    caller must commit later via ``diff.engine.commit_seen`` once the items are
    actually consumed (the news → synth deferred-commit path, issue #23).
    """
    try:
        state = DynamoDbState(source)
        if hasattr(state, "load"):
            state.load()
        was_empty = len(state.seen) == 0
        fresh = detect_new(source, docs, state=state, commit=commit)
        if seed_if_empty and was_empty and docs:
            print(
                f"Info: {source} baseline seeded ({len(docs)} items); "
                "new items will surface from the next run on."
            )
            return []
        return fresh
    except Exception as exc:  # pragma: no cover - defensive handling for state-table issues
        print(f"Warning: {source} diff state unavailable: {exc}")
        # Never dump a full authorization registry as "new" when state is broken.
        if seed_if_empty:
            return []
        return docs


def _moves_since_last_run(
    source: str,
    items: list[dict[str, Any]],
    key_field: str,
    value_field: str,
    min_pct: float,
) -> list[dict[str, Any]]:
    """Compare numeric series against DynamoDB value state; no inventing moves on failure."""
    try:
        state = DynamoDbValueState(source)
        return detect_moves(
            source,
            items,
            key_field=key_field,
            value_field=value_field,
            min_pct=min_pct,
            state=state,
        )
    except Exception as exc:  # pragma: no cover - defensive handling for state-table issues
        print(f"Warning: {source} value state unavailable, skipping moves: {exc}")
        return []


def _csv_env(name: str) -> list[str]:
    return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]


def _tag_new(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark delta rows for Stage B prioritization without mutating originals."""
    out: list[dict[str, Any]] = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        row = {k: v for k, v in d.items() if k != "raw"}
        row["is_new"] = True
        out.append(row)
    return out


def _strip_raw(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact rows for digest context (drop bulky raw payloads).

    SEC primary documents can be 100k+ chars; digests keep subject + a short
    excerpt so Stage B fusion stays light. Full text is on the raw S3 object.
    """
    max_text = int(os.environ.get("ONCA_DIGEST_TEXT_CHARS", "2000"))
    out: list[dict[str, Any]] = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        row = {k: v for k, v in d.items() if k != "raw"}
        row.setdefault("is_new", False)
        text = row.get("text")
        if isinstance(text, str) and len(text) > max_text:
            row["text"] = text[:max_text].rstrip() + "…"
        out.append(row)
    return out


# --- ADR 019 Phase 2 — registry-driven source runner ---------------------------------------
# The (gate → budget → fetch → delta → standard digest section) pattern, driven by a
# SourceSpec, replacing the per-source hand-coded blocks. Migration is source-by-source;
# the FS-core sources with interleaved side effects move over incrementally.

_VERTICAL_WARNED: set[str] = set()


def _active_vertical() -> str:
    """The market this deployment serves (ADR 019 Phase 3). Onça = financial-services;
    the Anteater sectorial deployment sets ONCA_VERTICAL to a sector."""
    v = os.environ.get("ONCA_VERTICAL", registry.VERTICAL_FS)
    if not registry.is_known_vertical(v) and v not in _VERTICAL_WARNED:
        _VERTICAL_WARNED.add(v)
        print(f"Warning: ONCA_VERTICAL={v!r} is not a known vertical "
              f"{registry.KNOWN_VERTICALS}; failing closed (only sector-agnostic sources run).")
    return v


def _vertical_ok(spec: "registry.SourceSpec") -> bool:
    """Whether ``spec`` applies to the active vertical (sector-agnostic = ``all``)."""
    return registry.ALL in spec.verticals or _active_vertical() in spec.verticals


def _source_enabled(spec: "registry.SourceSpec") -> bool:
    """Gate a source on its vertical applicability (ADR 019 Phase 3) then its env flag."""
    if not _vertical_ok(spec):
        return False
    default = "true" if spec.default_on else "false"
    flag = spec.env_flag or f"ONCA_{spec.id.upper()}"
    return os.environ.get(flag, default).lower() in ("1", "true", "yes")


def _lens_section(
    records: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
    spec: "registry.SourceSpec",
    **extra: Any,
) -> dict[str, Any]:
    """The standard {count, new_count, items, context} digest section, with per-source
    limits taken from the spec (declarative). ``extra`` adds source-specific keys."""
    return {
        "count": len(records),
        "new_count": len(new_records),
        **extra,
        "items": _tag_new(new_records[: spec.items_limit]),
        "context": _strip_raw(records[: spec.context_limit]),
    }


def _gated_source(
    spec: "registry.SourceSpec",
    *,
    deadline: float,
    per_source: int,
    fetch: "Any",
    store: "Any" = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run one registry source: gate → budget → fetch → delta → optional store.

    ``fetch()`` returns the source's records; ``store(records)`` persists a durable index
    when the source is store-integrated. Returns (records, new_records). Best-effort — a
    failure degrades to ([], []) like every other source here."""
    if not _source_enabled(spec):
        return [], []
    records: list[dict[str, Any]] = []
    new_records: list[dict[str, Any]] = []
    try:
        with _source_budget(spec.label or spec.id, deadline, per_source):
            records = fetch() or []
            new_records = _new_since_last_run(
                spec.state_key or spec.id, records, seed_if_empty=spec.seed_if_empty
            )
            if store is not None and records:
                store(records)
    except Exception as exc:  # pragma: no cover - defensive; upstream best-effort
        print(f"Warning: {spec.label or spec.id} fetch failed: {exc}")
    return records, new_records


def _populate_corpus_and_sync(new_docs: list[dict[str, Any]]) -> None:
    """Write new docs to the raw corpus bucket and trigger a KB ingestion sync.

    A corpus/sync failure must never break the digest response, matching the
    graceful-degradation pattern used for every other external call here.
    """
    raw_bucket = os.environ.get("ONCA_RAW_BUCKET")
    if not raw_bucket:
        return

    # Defense-in-depth: never write an unbounded number of objects in one run.
    # A broken diff state once marked every fund "new"; capping keeps a single
    # run's corpus writes (and the ingest Lambda's wall clock) bounded.
    max_docs = int(os.environ.get("ONCA_MAX_CORPUS_DOCS", "300"))
    if len(new_docs) > max_docs:
        print(
            f"Info: corpus write capped at {max_docs} of {len(new_docs)} new docs"
        )
        new_docs = new_docs[:max_docs]

    try:
        written = raw_writer.write_raw_documents(raw_bucket, new_docs)
    except Exception as exc:  # pragma: no cover - defensive handling for S3 write failures
        print(f"Warning: raw corpus write failed: {exc}")
        return

    if not written:
        return

    kb_id = os.environ.get("ONCA_KB_ID")
    data_source_id = os.environ.get("ONCA_KB_DATA_SOURCE_ID")
    if not kb_id or not data_source_id:
        return

    try:
        boto3.client("bedrock-agent").start_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=data_source_id
        )
    except Exception as exc:  # pragma: no cover - defensive handling for KB sync failures
        print(f"Warning: KB ingestion sync failed: {exc}")


def _news_slice(context: Any) -> dict[str, Any]:
    """Trade-press (Google News RSS + outlet feeds) news slice of the digest.

    Factored out so it can run as its OWN parallel Step Functions branch: news
    is the only source that scales linearly with the registry (one HTTP per
    term), so it no longer shares a wall-clock budget with the fixed-cost
    structured sources. News is not written to the corpus, so this path has no
    corpus/state coupling with the structured branch (disjoint diff sources)."""
    news_lookback = int(os.environ.get("ONCA_NEWS_LOOKBACK_DAYS", "14"))
    competitors = _csv_env("ONCA_COMPETITORS")
    news_terms = _csv_env("ONCA_NEWS_WATCHLIST")
    if os.environ.get("ONCA_NEWS_USE_COMPETITORS", "true").lower() in ("1", "true", "yes"):
        news_terms = list(dict.fromkeys(news_terms + competitors))
    # Derive news terms from the registry (source of truth for tracked entities)
    # so the news set can't drift out of sync with it (the C6/PicPay silence bug).
    if os.environ.get("ONCA_ENTITIES_TABLE") and os.environ.get(
        "ONCA_NEWS_USE_REGISTRY", "true"
    ).lower() in ("1", "true", "yes"):
        try:
            from src.synth import entity_registry

            reg_terms = entity_registry.news_terms()
            have = {t.lower() for t in news_terms}
            news_terms = news_terms + [t for t in reg_terms if t.lower() not in have]
            print(f"News terms: {len(reg_terms)} from registry, {len(news_terms)} total")
        except Exception as exc:  # pragma: no cover - best-effort, config still works
            print(f"Warning: registry news terms unavailable, using config: {exc}")

    deadline = _ingest_deadline(context)
    per_source = int(os.environ.get("ONCA_SOURCE_TIMEOUT_SEC", "90"))
    news_items: list[dict[str, Any]] = []
    new_news: list[dict[str, Any]] = []
    if news_terms:
        try:
            with _source_budget("Trade press", deadline, per_source):
                news_items = trade_press.fetch_news(
                    news_terms,
                    lookback_days=news_lookback,
                    max_terms=int(os.environ.get("ONCA_NEWS_MAX_TERMS", "80")),
                )
                # Deferred commit (issue #23): compute the fresh set but do NOT
                # mark anything seen here. Synth commits ``fetched_ids`` only
                # after it has consumed this slice, so a fetch-only run or a
                # failed/retried synth never burns the news — the items simply
                # re-surface next run instead of leaving the entity falsely
                # silent. Seed suppression on a truly-empty state still holds.
                new_news = _new_since_last_run(
                    "trade_press", news_items, seed_if_empty=True, commit=False
                )
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: trade-press fetch failed: {exc}")
    return {
        "count": len(news_items),
        "new_count": len(new_news),
        # These caps bound what SYNTH can see. The old 12/15 were sized for a
        # ~15-entity watchlist; at registry scale (60+ entities) the most-recent
        # slice is monopolised by a few high-volume names (BTG, BB, Bradesco),
        # starving low-volume entities (crypto, consórcio, advisory) of synth
        # visibility even when their news clears the corroboration gate. Persist
        # ALL new items (env-tunable) + a broad context sample; rows are compact.
        "items": _tag_new(new_news[: int(os.environ.get("ONCA_NEWS_DIGEST_ITEMS", "150"))]),
        "context": _strip_raw(news_items[: int(os.environ.get("ONCA_NEWS_DIGEST_CONTEXT", "60"))]),
        # Every fetched id (not just the capped items/context) so synth commits
        # the exact set it saw to the trade_press seen-set — the second phase of
        # the deferred diff above. Compact: bare id strings.
        "fetched_ids": [d["id"] for d in news_items if isinstance(d, dict) and d.get("id")],
    }


def _empty_news_slice() -> dict[str, Any]:
    return {"count": 0, "new_count": 0, "items": [], "context": [], "fetched_ids": []}


def _write_news_digest(slice_: dict[str, Any], context: Any) -> dict[str, Any]:
    """Persist the news slice to its own S3 prefix so synth can overlay it onto
    the structured base digest (the two branches write disjoint objects)."""
    payload = {"news": slice_, "source": "news_ingest"}
    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if bucket:
        try:
            s3 = boto3.client("s3")
            key = f"lambda-digests/news/{getattr(context, 'aws_request_id', 'local')}.json"
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
        except Exception as exc:  # pragma: no cover - defensive handling for S3 write failures
            print(f"Warning: S3 news upload failed: {exc}")
    return {"statusCode": 200, "body": json.dumps(payload, ensure_ascii=False)}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Return a small digest payload for downstream Lambda/CDK wiring.

    ``mode`` (event or ONCA_INGEST_MODE) splits the work into parallel branches:
      - "news":       fetch only trade-press → write the news slice to S3.
      - "structured": fetch every structured source (+ corpus) → write the base
                      digest to S3 with an empty news slice (news arrives via the
                      parallel news branch and is overlaid at synth time).
      - "all" (default): both, in one invocation (local / back-compat)."""
    mode = (event or {}).get("mode") or os.environ.get("ONCA_INGEST_MODE", "all")
    if mode == "news":
        return _write_news_digest(_news_slice(context), context)

    lookback_days = int(os.environ.get("ONCA_LOOKBACK_DAYS", "7"))
    competitors = _csv_env("ONCA_COMPETITORS")
    competitor_ispb = _csv_env("ONCA_COMPETITOR_ISPB")
    pix_threshold = float(os.environ.get("ONCA_PIX_MOVE_THRESHOLD_PCT", "15.0"))
    juros_competitors = _csv_env("ONCA_JUROS_COMPETITORS")
    juros_modalities = _csv_env("ONCA_JUROS_MODALITIES")
    juros_use_defaults = os.environ.get("ONCA_JUROS_USE_DEFAULT_MODALITIES", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    juros_threshold = float(os.environ.get("ONCA_JUROS_MOVE_THRESHOLD_PCT", "10.0"))
    ofertas_lookback = int(os.environ.get("ONCA_OFERTAS_LOOKBACK_DAYS", "30"))
    ofertas_watch = _csv_env("ONCA_OFERTAS_WATCHLIST")
    if not ofertas_watch and os.environ.get(
        "ONCA_OFERTAS_USE_COMPETITORS", "true"
    ).lower() in ("1", "true", "yes"):
        ofertas_watch = competitors
    sec_tickers = _csv_env("ONCA_SEC_TICKERS")
    sec_lookback = int(os.environ.get("ONCA_SEC_LOOKBACK_DAYS", "365"))
    inf_diario_watch = _csv_env("ONCA_INF_DIARIO_WATCHLIST")
    if not inf_diario_watch and os.environ.get(
        "ONCA_INF_DIARIO_USE_COMPETITORS", "true"
    ).lower() in ("1", "true", "yes"):
        inf_diario_watch = competitors
    inf_diario_threshold = float(
        os.environ.get("ONCA_INF_DIARIO_MOVE_THRESHOLD_PCT", "10.0")
    )
    inf_diario_top = os.environ.get("ONCA_INF_DIARIO_TOP_N", "").strip()
    inf_diario_top_n = int(inf_diario_top) if inf_diario_top else None

    # CVM material facts (Fato Relevante / Comunicado ao Mercado). Own watchlist
    # (B3-listed FS names); optionally unioned with the competitors list.
    fatos_lookback = int(os.environ.get("ONCA_FATOS_LOOKBACK_DAYS", "45"))
    fatos_watch = _csv_env("ONCA_FATOS_WATCHLIST")
    if os.environ.get("ONCA_FATOS_USE_COMPETITORS", "true").lower() in ("1", "true", "yes"):
        fatos_watch = list(dict.fromkeys(fatos_watch + competitors))
    # Derive the structured (Fato Relevante / Comunicado ao Mercado) watchlist from
    # the registry too, so a newly curated B3-listed entity (one carrying a
    # fatos_term) gets a STRUCTURED lens with no redeploy — mirrors the news-terms
    # derivation below. Structured identity means such an entity resolves from its
    # CVM filing and does not depend on the fragile news-only corroboration gate
    # (nor on an ambiguous place-name brand like "Porto Seguro").
    if os.environ.get("ONCA_ENTITIES_TABLE") and os.environ.get(
        "ONCA_FATOS_USE_REGISTRY", "true"
    ).lower() in ("1", "true", "yes"):
        try:
            from src.synth import entity_registry

            reg_fatos = entity_registry.fatos_terms()
            have = {t.lower() for t in fatos_watch}
            fatos_watch = fatos_watch + [t for t in reg_fatos if t.lower() not in have]
            print(f"Fatos terms: {len(reg_fatos)} from registry, {len(fatos_watch)} total")
        except Exception as exc:  # pragma: no cover - best-effort, config still works
            print(f"Warning: registry fatos terms unavailable, using config: {exc}")
    fatos_categories = _csv_env("ONCA_FATOS_CATEGORIES") or None

    # Diário Oficial (DOU) — SUSEP / CADE / BACEN acts mentioning a competitor.
    dou_lookback = int(os.environ.get("ONCA_DOU_LOOKBACK_DAYS", "30"))
    dou_terms = _csv_env("ONCA_DOU_WATCHLIST")
    if os.environ.get("ONCA_DOU_USE_COMPETITORS", "true").lower() in ("1", "true", "yes"):
        dou_terms = list(dict.fromkeys(dou_terms + fatos_watch))
    # Betting/iGaming structured lens: the SPA (Secretaria de Prêmios e Apostas,
    # Min. Fazenda) publishes NO clean list API — its authorisations/sanctions
    # are published as SPA/MF acts in the DOU, which we already parse. These
    # thematic terms surface those acts (the new-authorisation event) and tag an
    # operator when the act names one. Toggle with ONCA_DOU_BETTING.
    if os.environ.get("ONCA_DOU_BETTING", "true").lower() in ("1", "true", "yes"):
        dou_terms = list(dict.fromkeys(
            dou_terms + ["Secretaria de Prêmios e Apostas", "apostas de quota fixa"]
        ))

    # Trade-press news is fetched by _news_slice (its own parallel branch); see
    # the mode dispatch at the top of lambda_handler.

    # Wall-clock guards: bound each source and stop starting new work before the
    # Lambda times out, so ingest always returns a (possibly partial) digest.
    deadline = _ingest_deadline(context)
    per_source = int(os.environ.get("ONCA_SOURCE_TIMEOUT_SEC", "90"))

    # regulatory (bcb_normativos) + competitor (cvm_fundos) are now produced by the registry
    # loop below (ADR 019 — the FETCHERS registry), together with ofertas/dou/cade/sanctions/
    # contracts. Their outputs are extracted from `loop_results` right before the digest.

    try:
        with _source_budget("IF.data market", deadline, per_source):
            base_date = bcb_ifdata.latest_base_date()
            rows = bcb_ifdata.fetch_institutions(base_date=base_date)
            names = bcb_ifdata.fetch_institution_names(base_date)
            shares = bcb_ifdata.market_share(rows, institution_names=names)
            market = shares[:10]
            # ADR 015 §3: resolve institution names -> entity_id and persist a durable
            # bcb_ifdata/index.json store so feed_builder can emit entities[].market_
            # share_pct. Best-effort, mirrors the bcb_reclamacoes store below.
            bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
            if bucket:
                from src.synth.entities import resolve_entities

                recs = bcb_ifdata.map_to_entities(
                    shares, resolver=resolve_entities, base_date=base_date
                )
                if recs:
                    bcb_ifdata.update_store(recs, bucket)
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        market = []
        print(f"Warning: IF.data market fetch failed: {exc}")

    # New entrants — authorized-entities registry (seed suppressed on first run).
    authorized: list[dict[str, Any]] = []
    new_entrants: list[dict[str, Any]] = []
    try:
        with _source_budget("BCB autorizações", deadline, per_source):
            authorized = bcb_autorizacoes.fetch_authorized()
            new_entrants = _new_since_last_run(
                "bcb_autorizacoes", authorized, seed_if_empty=True
            )
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        print(f"Warning: BCB autorizações fetch failed: {exc}")

    # Receita Federal enrichment: resolve the brand + controllers behind each new
    # entrant's (otherwise anonymous) CNPJ. Own budget so a slow lookup can't lose
    # the entrants list; only new entrants (bounded volume) are enriched.
    if new_entrants and os.environ.get("ONCA_RECEITA_ENRICH", "true").lower() in (
        "1",
        "true",
        "yes",
    ):
        try:
            with _source_budget("Receita QSA", deadline, per_source):
                receita_cnpj.enrich_entrants(new_entrants)
        except Exception as exc:  # pragma: no cover - enrichment is best-effort
            print(f"Warning: Receita enrichment skipped: {exc}")

    # Entities registry auto-create (ADR step 3): give each new fintech entrant's
    # CNPJ a registry record so future signals about it (CVM/news/DOU) resolve and
    # cluster with no redeploy. CNPJ-keyed + idempotent; best-effort, never blocks.
    if (
        new_entrants
        and os.environ.get("ONCA_ENTITIES_TABLE")
        and os.environ.get("ONCA_ENTITIES_AUTOCREATE", "true").lower()
        in ("1", "true", "yes")
    ):
        fintech_only = os.environ.get(
            "ONCA_ENTITIES_AUTOCREATE_FINTECH_ONLY", "true"
        ).lower() in ("1", "true", "yes")
        try:
            with _source_budget("entities auto-create", deadline, per_source):
                from src.synth import entity_registry

                review_on = os.environ.get("ONCA_ENTITIES_REVIEW", "true").lower() in (
                    "1",
                    "true",
                    "yes",
                )
                created = 0
                for e in new_entrants:
                    if fintech_only and not e.get("is_fintech"):
                        continue
                    eid = entity_registry.auto_create_from_entrant(e)
                    if eid:
                        e["registry_entity_id"] = eid
                        created += 1
                        # New entity's bare brand stays structured-only until a
                        # curator vets it (ADR 002). Queue a news-safe promotion
                        # review when the brand is a single token (multi-token
                        # legal names are already distinctive — no gate to lift).
                        brand = str(e.get("trade_name") or e.get("name") or "").strip()
                        if review_on and brand and " " not in brand:
                            entity_registry.propose_news_safe(eid, brand)
                        # Ambiguous license → industry couldn't be auto-assigned.
                        # Queue a curator pick so an add-on for this module still
                        # aggregates the entrant's signals (ADR 002 Phase B).
                        if review_on:
                            _inds, needs = entity_registry.classify_industries(e)
                            if needs:
                                entity_registry.propose_industry(
                                    eid, brand or str(e.get("name") or "")
                                )
                if created:
                    print(f"entities: auto-created {created} from new entrants")
                    # ADR step 5: a fresh entity may share a QSA controller with
                    # an existing one — queue group-merge proposals for review
                    # (never auto-merged). Only when something was created, so
                    # the table scan stays off the common no-new-entrant runs.
                    if review_on:
                        queued = entity_registry.propose_group_merges()
                        if queued:
                            print(f"entities: queued {queued} group-merge reviews")
        except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
            print(f"Warning: entity auto-create skipped: {exc}")

    # Entity discovery — structured CVM FIAGRO universe → registry (ADR 011 / #14).
    # High-precision path: every row has a CNPJ. Auto-creates / enriches under
    # industry agri-funds. Gated OFF by default until the first live validation
    # (set ONCA_ENTITY_DISCOVERY=true). Weekly-ish cost is fine; runs best-effort
    # inside the same budget window as the rest of ingest.
    if (
        os.environ.get("ONCA_ENTITIES_TABLE")
        and os.environ.get("ONCA_ENTITY_DISCOVERY", "false").lower()
        in ("1", "true", "yes")
    ):
        try:
            with _source_budget("entity discovery FIAGRO", deadline, per_source):
                from src.synth import entity_discovery

                min_pl = float(os.environ.get("ONCA_FIAGRO_MIN_PL", "50000000"))
                auto = os.environ.get(
                    "ONCA_ENTITY_DISCOVERY_AUTOCREATE", "true"
                ).lower() in ("1", "true", "yes")
                report = entity_discovery.discover_fiagro(
                    min_pl=min_pl, auto_create=auto, max_new=40
                )
                print(
                    "entity discovery FIAGRO: "
                    f"fetched={report.get('fetched')} "
                    f"created={len(report.get('created') or [])} "
                    f"enriched={len(report.get('enriched') or [])} "
                    f"already={report.get('already')} "
                    f"proposed={len(report.get('proposed') or [])}"
                )
        except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
            print(f"Warning: entity discovery skipped: {exc}")

        # Consórcio administradoras — structured BCB universe → registry (issue #46,
        # ADR 017). Same master gate; every row has a CNPJ. Conglomerate arms
        # (Itaú/Bradesco/Santander/Porto Seguro) nest as sub-entities of the tier-1 parent.
        try:
            with _source_budget("entity discovery consórcio", deadline, per_source):
                from src.synth import entity_discovery

                auto = os.environ.get(
                    "ONCA_ENTITY_DISCOVERY_AUTOCREATE", "true"
                ).lower() in ("1", "true", "yes")
                creport = entity_discovery.discover_consorcio(auto_create=auto, max_new=100)
                print(
                    "entity discovery consórcio: "
                    f"fetched={creport.get('fetched')} "
                    f"created={len(creport.get('created') or [])} "
                    f"enriched={len(creport.get('enriched') or [])} "
                    f"already={creport.get('already')} "
                    f"proposed={len(creport.get('proposed') or [])}"
                )
        except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
            print(f"Warning: consórcio discovery skipped: {exc}")

        # BCB-authorized institutions → registry (#14 Official Registry Sync). Behind an
        # EXTRA sub-gate (ONCA_DISCOVER_BCB, default off even when discovery is on): the
        # BCB registry is huge, so this stays dark until a dry-run validates brand quality.
        # Relevance-gated to banks/IP/SCD-SEP-SCFI/corretoras; cooperativas need their own
        # flag (thousands of singulars). Conglomerate arms nest under the tier-1 parent.
        if os.environ.get("ONCA_DISCOVER_BCB", "false").lower() in ("1", "true", "yes"):
            try:
                with _source_budget("entity discovery BCB", deadline, per_source):
                    from src.synth import entity_discovery

                    auto = os.environ.get(
                        "ONCA_ENTITY_DISCOVERY_AUTOCREATE", "true"
                    ).lower() in ("1", "true", "yes")
                    coops = os.environ.get(
                        "ONCA_DISCOVER_BCB_COOPS", "false"
                    ).lower() in ("1", "true", "yes")
                    breport = entity_discovery.discover_bcb_institutions(
                        auto_create=auto, include_coops=coops, max_new=40)
                    print(
                        "entity discovery BCB: "
                        f"fetched={breport.get('fetched')} "
                        f"created={len(breport.get('created') or [])} "
                        f"enriched={len(breport.get('enriched') or [])} "
                        f"already={breport.get('already')} "
                        f"proposed={len(breport.get('proposed') or [])} "
                        f"skipped={len(breport.get('skipped') or [])}"
                    )
            except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
                print(f"Warning: BCB institutions discovery skipped: {exc}")

    # Financial statements (issue #7 / ADR 011 stage 6): CVM DFP → per-issuer key-metric
    # store (financials/index.json). Filings are annual/quarterly, so this is a periodic
    # best-effort refresh, gated OFF by default (ONCA_FINANCIALS=true to enable).
    if os.environ.get("ONCA_FINANCIALS", "false").lower() in ("1", "true", "yes"):
        try:
            with _source_budget("financials DFP", deadline, per_source):
                import datetime as _dt

                from src.ingest import cvm_financials
                from src.synth import entities as _ent
                from src.synth import entity_registry as _er

                bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
                year = int(os.environ.get("ONCA_FINANCIALS_YEAR", str(_dt.date.today().year - 1)))
                stmts = cvm_financials.fetch_statements(year, doc="DFP")
                idx = cvm_financials.build_index(
                    list(_er.list_entities(include_inactive=True)), stmts,
                    resolver=_ent.resolve_entities,
                )
                if idx and bucket:
                    cvm_financials.persist(bucket, idx)
                print(f"financials DFP {year}: issuers={len(stmts)} matched={len(idx)}")
        except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
            print(f"Warning: financials ingest skipped: {exc}")

    # FIAGRO agri-funds — PL / cotista moves + newly-registered classes as
    # narrative-ready signal (task b). Entity discovery above only populates the
    # registry; without this, 0 agri-funds narratives ever reach synth. Reuses
    # the generic value-state move engine (same primitives as Pix/juros/Informe
    # Diário) — first run seeds a baseline only. Independently gated from
    # ONCA_ENTITY_DISCOVERY so either can be disabled without the other.
    fiagro_rows: list[dict[str, Any]] = []
    fiagro_pl_moves: list[dict[str, Any]] = []
    fiagro_cotista_moves: list[dict[str, Any]] = []
    fiagro_new_regs: list[dict[str, Any]] = []
    if os.environ.get("ONCA_FIAGRO_SIGNAL", "true").lower() in ("1", "true", "yes"):
        try:
            with _source_budget("FIAGRO moves", deadline, per_source):
                fiagro_min_pl = float(os.environ.get("ONCA_FIAGRO_MIN_PL", "50000000"))
                pl_threshold = float(
                    os.environ.get("ONCA_FIAGRO_PL_MOVE_THRESHOLD_PCT", "15.0")
                )
                cotista_threshold = float(
                    os.environ.get("ONCA_FIAGRO_COTISTA_MOVE_THRESHOLD_PCT", "30.0")
                )
                reg_lookback = int(
                    os.environ.get("ONCA_FIAGRO_NEW_REG_LOOKBACK_DAYS", "60")
                )
                fiagro_rows = cvm_fiagro.fetch_fiagro(min_pl=fiagro_min_pl)
                fiagro_pl_moves = _moves_since_last_run(
                    "cvm_fiagro_pl",
                    cvm_fiagro.for_pl_moves(fiagro_rows),
                    key_field="cnpj",
                    value_field="pl",
                    min_pct=pl_threshold,
                )
                fiagro_cotista_moves = _moves_since_last_run(
                    "cvm_fiagro_cotistas",
                    cvm_fiagro.for_cotista_moves(fiagro_rows),
                    key_field="cnpj",
                    value_field="cotistas",
                    min_pct=cotista_threshold,
                )
                reg_candidates = cvm_fiagro.for_new_registrations(
                    fiagro_rows, lookback_days=reg_lookback
                )
                fiagro_new_regs = _new_since_last_run(
                    "cvm_fiagro_newreg", reg_candidates, seed_if_empty=True
                )
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: CVM FIAGRO moves fetch failed: {exc}")
    fiagro_moves_all = fiagro_pl_moves + fiagro_cotista_moves + fiagro_new_regs
    # Attribute each FIAGRO move to exactly ONE entity by CNPJ — a structured id
    # that is authoritative. This bypasses the free-text alias matcher, which
    # over-resolves FIAGRO funds via their shared *administrator* name (dozens of
    # funds share one DTVM/corretora admin, so a single move would otherwise fan
    # out to every co-administered fund). Drop a move whose CNPJ isn't yet a
    # registry entity — there's no subject to attribute it to (discovery adds the
    # fund later, after which its next move resolves).
    if fiagro_moves_all:
        try:
            from src.synth import entity_registry as _fiagro_reg

            _resolved: list[dict[str, Any]] = []
            for _ev in fiagro_moves_all:
                _eid = _fiagro_reg.resolve_by_cnpj(str(_ev.get("cnpj") or ""))
                if _eid:
                    _ev["_entities"] = [_eid]
                    _resolved.append(_ev)
            fiagro_moves_all = _resolved
        except Exception as exc:  # pragma: no cover - never emit unattributed fan-out
            print(f"Warning: FIAGRO entity resolution failed, dropping signal: {exc}")
            fiagro_moves_all = []

    # Pix traction — month-over-month volume moves (first run seeds baseline only).
    pix_by_inst: list[dict[str, Any]] = []
    pix_moves: list[dict[str, Any]] = []
    try:
        with _source_budget("BCB Pix", deadline, per_source):
            pix_rows = bcb_pix.fetch_recent()
            pix_by_inst = bcb_pix.by_institution(
                pix_rows, watchlist_ispb=competitor_ispb or None
            )
            pix_moves = _moves_since_last_run(
                "bcb_pix",
                pix_by_inst,
                key_field="ispb",
                value_field="tx_value",
                min_pct=pix_threshold,
            )
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        print(f"Warning: BCB Pix fetch failed: {exc}")

    # Juros médios — relative rate moves by institution × modality.
    juros_focus: list[dict[str, Any]] = []
    juros_moves: list[dict[str, Any]] = []
    try:
        with _source_budget("BCB juros médios", deadline, per_source):
            juros_rows = bcb_juros.fetch_daily()
            modalities = juros_modalities
            if not modalities and juros_use_defaults:
                modalities = list(bcb_juros.DEFAULT_MODALITY_FILTERS)
            juros_focus = bcb_juros.filter_rates(
                juros_rows,
                institutions=juros_competitors or None,
                modalities=modalities or None,
            )
            juros_moves = _moves_since_last_run(
                "bcb_juros",
                bcb_juros.for_moves(juros_focus),
                key_field="move_key",
                value_field="rate_year",
                min_pct=juros_threshold,
            )
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        print(f"Warning: BCB juros médios fetch failed: {exc}")

    # CVM ofertas — produced by the registry loop below.

    # CVM material facts — Fato Relevante / Comunicado ao Mercado (strategic
    # disclosures from B3-listed competitors; seed suppressed on first run).
    fatos: list[dict[str, Any]] = []
    new_fatos: list[dict[str, Any]] = []
    if fatos_watch:
        try:
            with _source_budget("CVM fatos relevantes", deadline, per_source):
                fatos = cvm_ipe.fetch_material_facts(
                    lookback_days=fatos_lookback,
                    watchlist=fatos_watch,
                    categories=fatos_categories,
                )
                new_fatos = _new_since_last_run("cvm_fatos", fatos, seed_if_empty=True)
                # Governance events are the strategic ones — float them to the
                # front so they survive the digest item cap and reach synth.
                new_fatos.sort(key=lambda f: not f.get("governance"))
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: CVM fatos relevantes fetch failed: {exc}")

    # Entities registry alias accumulation (ADR step 4): a structured CVM signal
    # (offering issuer / fato relevante company) that carries a CNPJ already
    # resolving to a known entity contributes its razão social to that entity's
    # aliases — so a later name-only signal (news, DOU) about it resolves too.
    # Data-derived + CNPJ-gated is the auto-safe case; best-effort, never blocks.
    if os.environ.get("ONCA_ENTITIES_TABLE") and os.environ.get(
        "ONCA_ENTITIES_ACCUMULATE", "true"
    ).lower() in ("1", "true", "yes"):
        try:
            with _source_budget("entities alias accumulation", deadline, per_source):
                from src.synth import entity_registry

                named = [(o.get("issuer"), o.get("issuer_cnpj")) for o in new_ofertas]
                named += [(f.get("company"), f.get("cnpj")) for f in new_fatos]
                acc = 0
                seen_pairs: set[tuple[str, str]] = set()
                for name, cnpj in named:
                    root = "".join(ch for ch in str(cnpj or "") if ch.isdigit())[:8]
                    if not name or len(root) < 8:
                        continue
                    pair = (root, str(name))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    eid = entity_registry.resolve_by_cnpj(root)
                    if eid and entity_registry.accumulate_aliases(eid, [name]):
                        acc += 1
                if acc:
                    print(f"entities: accumulated aliases for {acc} CVM signals")
        except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
            print(f"Warning: entity alias accumulation skipped: {exc}")

    # Diário Oficial (dou) — produced by the registry loop below.

    # CADE antitrust (#61) — produced by the registry loop below (fetch resolves the merger
    # parties by name; see the "antitrust" fetcher).
    from src.synth.entities import resolve_entities as _resolve_entities

    # BCB macro — Copom/Selic decision + weekly Focus expectations (market-wide,
    # not entity-tied; surfaces as standalone "macro" cards). Best-effort.
    macro_selic: dict[str, Any] | None = None
    macro_focus: list[dict[str, Any]] = []
    if os.environ.get("ONCA_MACRO", "true").lower() in ("1", "true", "yes"):
        try:
            with _source_budget("BCB macro", deadline, per_source):
                macro_selic = bcb_macro.fetch_selic()
                macro_focus = bcb_macro.fetch_focus()
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: BCB macro fetch failed: {exc}")

    # DataJud (CNJ) — corporate-distress filings (recuperação judicial / falência).
    # Party-name-scrubbed public API, so this is a MACRO sector-distress signal
    # (filing volume/trend), not entity-tied (issue #25). Best-effort; off via env.
    distress_summary: dict[str, Any] | None = None
    if os.environ.get("ONCA_DATAJUD", "true").lower() in ("1", "true", "yes"):
        try:
            with _source_budget("DataJud RJ", deadline, per_source):
                tribs = _csv_env("ONCA_DATAJUD_TRIBUNALS") or list(datajud.DEFAULT_TRIBUNALS)
                distress = datajud.fetch_recuperacao_judicial(
                    tribs, lookback_days=int(os.environ.get("ONCA_DATAJUD_LOOKBACK_DAYS", "90"))
                )
                new_distress = _new_since_last_run("datajud_rj", distress, seed_if_empty=True)
                distress_summary = {
                    **datajud.summarize(distress),
                    "new_count": len(new_distress),
                    "items": _tag_new(new_distress[:12]),
                }
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: DataJud fetch failed: {exc}")

    # BCB complaints ranking — OFFICIAL quarterly consumer-complaints ranking per
    # institution (issue #31, the Reclame Aqui alternative). Public Olinda OData,
    # entity-tied; writes a durable bcb_reclamacoes/index.json store. Best-effort.
    bcb_reclamacoes_summary: dict[str, Any] | None = None
    if os.environ.get("ONCA_BCB_RECLAMACOES", "true").lower() in ("1", "true", "yes"):
        bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
        try:
            with _source_budget("BCB reclamações", deadline, per_source):
                from src.synth.entities import resolve_entities

                rows = bcb_reclamacoes.fetch_ranking()
                recs = bcb_reclamacoes.map_to_entities(rows, resolver=resolve_entities)
                if recs and bucket:
                    bcb_reclamacoes.update_store(recs, bucket)
                bcb_reclamacoes_summary = {
                    **bcb_reclamacoes.summarize(recs),
                    "items": recs[:12],
                }
        except Exception as exc:  # pragma: no cover - defensive; upstream best-effort
            print(f"Warning: BCB reclamações fetch failed: {exc}")

    # consumidor.gov.br complaints (#63) — cross-industry consumer reputation, the general
    # form of bcb_reclamacoes. DEFAULT-OFF + token-gated: the source is resolved via the
    # dados.gov.br catalog ([[gov_dados]]), whose GOV_DADOS_TOKEN is currently rejected, so
    # this is inert until the token is regenerated. Durable store, like bcb_reclamacoes.
    consumidor_gov_summary: dict[str, Any] | None = None
    if os.environ.get("ONCA_CONSUMIDOR_GOV", "false").lower() in ("1", "true", "yes"):
        bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
        try:
            with _source_budget("consumidor.gov", deadline, per_source):
                from src.synth.entities import resolve_entities

                rows = consumidor_gov.fetch_indicators(
                    min_complaints=int(os.environ.get("ONCA_CONSUMIDOR_MIN_COMPLAINTS", "30")))
                recs = consumidor_gov.map_to_entities(rows, resolver=resolve_entities)
                if recs and bucket:
                    consumidor_gov.update_store(recs, bucket)
                consumidor_gov_summary = {**consumidor_gov.summarize(recs), "items": recs[:12]}
        except Exception as exc:  # pragma: no cover - defensive; upstream best-effort
            print(f"Warning: consumidor.gov fetch failed: {exc}")

    # CEIS/CNEP federal sanctions (#60) — counterparty-integrity, sector-agnostic. Only
    # sanctions resolving (CNPJ-root first) to a tracked entity are kept; a durable
    # sanctions/index.json store holds state and _new_since_last_run drives the delta
    # (seed-suppressed so the historical backlog does not flood the first run).
    def _fetch_sanctions() -> list[dict[str, Any]]:
        from src.synth import entity_registry

        # CNPJ-only resolution — no registry, no signal (a sanction binds to a specific
        # legal person; see ceis_cnep docstring).
        idx = ceis_cnep.build_cnpj_index(
            entity_registry.list_entities()
        ) if os.environ.get("ONCA_ENTITIES_TABLE") else {}
        srows = ceis_cnep.fetch_sanctions() if idx else []
        return ceis_cnep.map_to_entities(srows, cnpj_index=idx)

    _bucket = os.environ.get("ONCA_DIGESTS_BUCKET")

    def _fetch_contracts() -> list[dict[str, Any]]:
        from src.synth import entity_registry

        idx = ceis_cnep.build_cnpj_index(
            entity_registry.list_entities()
        ) if os.environ.get("ONCA_ENTITIES_TABLE") else {}
        crows = pncp_contratos.fetch_contracts(
            days_back=int(os.environ.get("ONCA_PNCP_LOOKBACK_DAYS", "2")),
            max_pages=int(os.environ.get("ONCA_PNCP_MAX_PAGES", "60")),
            min_valor=float(os.environ.get("ONCA_PNCP_MIN_VALOR", "0")),
        ) if idx else []
        return pncp_contratos.map_to_entities(crows, cnpj_index=idx)

    # ADR 019 — the registry-driven ingest loop. Each standard "document" lens source is one
    # entry in FETCHERS (its fetch, keyed by spec.id); the loop applies vertical gating + the
    # wall-clock budget + delta + section-building UNIFORMLY. Adding such a source = a
    # SourceSpec + one FETCHERS entry, with no bespoke handler block and no payload edit.
    # (The numeric "moves" sources and the special-shape/side-effecting ones remain bespoke
    # below — they have distinct mechanics; migrating them is the tracked follow-on.)
    _cade_lookback = int(os.environ.get("ONCA_CADE_LOOKBACK_DAYS", "45"))
    _FETCHERS: dict[str, Any] = {
        "regulatory": lambda: bcb_normativos.fetch_recent(days=lookback_days),
        "competitor": lambda: cvm_fundos.fetch_funds(watchlist_admins=competitors),
        "ofertas": lambda: cvm_ofertas.fetch_recent(
            lookback_days=ofertas_lookback, watchlist=ofertas_watch or None),
        "dou": lambda: dou.fetch_dou(dou_terms, lookback_days=dou_lookback) if dou_terms else [],
        "cade": lambda: cade.map_to_entities(
            cade.fetch_atos(lookback_days=_cade_lookback), resolver=_resolve_entities),
        "sanctions": _fetch_sanctions,
        "contracts": _fetch_contracts,
    }
    _STORES: dict[str, Any] = {
        "sanctions": lambda recs: ceis_cnep.update_store(recs, _bucket) if _bucket else None,
        "contracts": lambda recs: pncp_contratos.update_store(recs, _bucket) if _bucket else None,
    }
    _SECTION_EXTRA: dict[str, Any] = {}  # per-source extra keys (e.g. fatos governance_count)
    loop_results: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    loop_sections: dict[str, dict[str, Any]] = {}
    for _spec in registry.active(_active_vertical()):
        _fetch = _FETCHERS.get(_spec.id)
        if _fetch is None:  # not (yet) loop-migrated — handled by a bespoke block
            continue
        _records, _new = _gated_source(
            _spec, deadline=deadline, per_source=per_source,
            fetch=_fetch, store=_STORES.get(_spec.id),
        )
        loop_results[_spec.id] = (_records, _new)
        loop_sections[_spec.id] = _lens_section(
            _records, _new, _spec, **_SECTION_EXTRA.get(_spec.id, {}))

    # Extract for the payload/corpus. A vertical-gated-out source yields ([], []).
    normativos, new_normativos = loop_results.get("regulatory", ([], []))
    funds, new_funds = loop_results.get("competitor", ([], []))
    offerings, new_ofertas = loop_results.get("ofertas", ([], []))
    dou_acts, new_dou = loop_results.get("dou", ([], []))
    cade_records, new_cade = loop_results.get("cade", ([], []))
    sanctions_records, new_sanctions = loop_results.get("sanctions", ([], []))
    contracts_records, new_contracts = loop_results.get("contracts", ([], []))

    # Reclame Aqui — consumer-reputation snapshots for retail-facing banks/fintechs
    # (issue #31). Unofficial source, so best-effort + off via env; writes a durable
    # reputation/index.json (entity-tied), surfaced in the feed + agent. Requires the
    # digests bucket (already available for the corpus/digest writes).
    # Default OFF: the public RA origin is Cloudflare-gated (403). Enable only with
    # an authorized/proxied endpoint via ONCA_RA_IOSEARCH (see reclame_aqui docstring).
    reputation_summary: dict[str, Any] | None = None
    if os.environ.get("ONCA_RECLAME_AQUI", "false").lower() in ("1", "true", "yes"):
        bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
        try:
            with _source_budget("Reclame Aqui", deadline, per_source):
                ids = _csv_env("ONCA_RA_ENTITY_IDS") or list(reclame_aqui.DEFAULT_ENTITY_IDS)
                comps = reclame_aqui.companies_from_registry(ids)
                snaps = reclame_aqui.fetch_reputation(comps)
                if snaps and bucket:
                    reclame_aqui.update_store(snaps, bucket)
                reputation_summary = {
                    **reclame_aqui.summarize(snaps),
                    "items": snaps[:12],
                }
        except Exception as exc:  # pragma: no cover - defensive; unofficial upstream
            print(f"Warning: Reclame Aqui fetch failed: {exc}")

    # SEC EDGAR — US-listed payments/fintech disclosures (seed on first run).
    # Metadata first (submissions JSON), then primary-document bodies for the
    # diffed set + a small context sample so digests/corpus carry text not only URLs.
    sec_filings_rows: list[dict[str, Any]] = []
    new_sec: list[dict[str, Any]] = []
    if sec_tickers:
        try:
            with _source_budget("SEC EDGAR", deadline, per_source):
                sec_filings_rows = sec_filings.fetch_filings(
                    sec_tickers, lookback_days=sec_lookback
                )
                new_sec = _new_since_last_run(
                    "sec_filings", sec_filings_rows, seed_if_empty=True
                )
                # Corpus: full body for genuinely new filings.
                if new_sec:
                    try:
                        sec_filings.enrich_with_content(new_sec)
                    except _SourceBudgetExceeded:
                        raise
                    except Exception as exc:  # pragma: no cover
                        print(f"Warning: SEC primary-document fetch failed: {exc}")
                # Digest context: enrich a few recent rows still missing text
                # (same object refs as new_sec are skipped via skip_existing).
                context_need = [r for r in sec_filings_rows[:15] if not r.get("text")]
                if context_need:
                    try:
                        sec_filings.enrich_with_content(context_need)
                    except _SourceBudgetExceeded:
                        raise
                    except Exception as exc:  # pragma: no cover
                        print(f"Warning: SEC context content fetch failed: {exc}")
        except Exception as exc:  # pragma: no cover
            print(f"Warning: SEC EDGAR fetch failed: {exc}")

    # CVM Informe Diário — fund AUM moves for watchlisted admins.
    inf_diario_rows: list[dict[str, Any]] = []
    inf_diario_moves: list[dict[str, Any]] = []
    if inf_diario_watch:
        try:
            with _source_budget("CVM Informe Diário", deadline, per_source):
                inf_diario_rows = cvm_inf_diario.fetch_latest(
                    watchlist_admins=inf_diario_watch,
                    top_n=inf_diario_top_n,
                )
                inf_diario_moves = _moves_since_last_run(
                    "cvm_inf_diario",
                    cvm_inf_diario.for_moves(inf_diario_rows),
                    key_field="move_key",
                    value_field="pl",
                    min_pct=inf_diario_threshold,
                )
        except Exception as exc:  # pragma: no cover
            print(f"Warning: CVM Informe Diário fetch failed: {exc}")

    # Corpus gets document-like signals only (not numeric Pix/juros/AUM moves).
    _populate_corpus_and_sync(
        new_normativos + new_funds + new_entrants + new_ofertas + new_sec
        + new_fatos + new_dou
    )

    # Stage B fusion needs more than deltas: after seeding, items[] is often
    # empty while pulls still succeed. Attach compact context samples and
    # tag delta rows with is_new for prioritization.
    payload = {
        # ADR 019 — sections for the registry-loop sources (regulatory/competitor/ofertas/
        # dou/cade/sanctions/contracts) are built IN the loop; splice them in. The bespoke
        # sources below still build their own sections (special shapes / side effects).
        **loop_sections,
        "market": {"count": len(market), "items": market, "context": market},
        "new_entrants": {
            "count": len(authorized),
            "new_count": len(new_entrants),
            "items": _tag_new(new_entrants[:8]),
            "context": _strip_raw(new_entrants[:8] or authorized[:5]),
        },
        "pix_moves": {
            "institutions_tracked": len(pix_by_inst),
            "move_count": len(pix_moves),
            "items": _tag_new(pix_moves[:10]),
            "context": _strip_raw(pix_by_inst[:15]),
        },
        "juros_moves": {
            "series_tracked": len(juros_focus),
            "move_count": len(juros_moves),
            "items": _tag_new(juros_moves[:10]),
            "context": _strip_raw(juros_focus[:15]),
        },
        # sec_filings + fatos are still bespoke (corpus write / alias accrual side effects).
        "sec_filings": _lens_section(sec_filings_rows, new_sec, registry.by_id("sec_filings")),
        "fatos": _lens_section(
            fatos, new_fatos, registry.by_id("fatos"),
            governance_count=sum(1 for f in new_fatos if f.get("governance")),
        ),
        # In "all" mode news is fetched inline here; in "structured" mode the news
        # slice is empty and the parallel news branch supplies it (overlaid at synth).
        "news": _news_slice(context) if mode == "all" else _empty_news_slice(),
        # Market-wide macro (Copom/Selic + Focus) — standalone macro cards.
        "macro": {"selic": macro_selic, "focus": macro_focus, "distress": distress_summary},
        # Consumer reputation (#31) — entity-tied; stores are authoritative.
        "reputation": reputation_summary,
        "bcb_reclamacoes": bcb_reclamacoes_summary,
        "consumidor_gov": consumidor_gov_summary,  # #63 (default-off, token-gated)
        "inf_diario_moves": {
            "funds_tracked": len(inf_diario_rows),
            "as_of": (inf_diario_rows[0].get("date") if inf_diario_rows else None),
            "move_count": len(inf_diario_moves),
            "items": _tag_new(inf_diario_moves[:10]),
            # Top-by-PL sample for entity fusion (already sorted in fetch_latest).
            "context": _strip_raw(inf_diario_rows[:15]),
        },
        # Agri-funds (FIAGRO) moves — task b. Reuses the "funds" lens (candidates.py
        # _collect_signals) so a solo material move can alert on its own, the same
        # as a new CVM fund-class filing.
        "fiagro_moves": {
            "funds_tracked": len(fiagro_rows),
            "pl_move_count": len(fiagro_pl_moves),
            "cotista_move_count": len(fiagro_cotista_moves),
            "new_registration_count": len(fiagro_new_regs),
            "move_count": len(fiagro_moves_all),
            "items": _tag_new(fiagro_moves_all[:12]),
            "context": _strip_raw(fiagro_rows[:15]),
        },
        "source": "lambda_port",
    }

    # ADR 019 Phase 3 — vertical gating: drop the lens sections of sources not applicable to
    # the active vertical (each digest section key IS the source id). Sector-agnostic sources
    # ({all}: sanctions/cade/contracts) survive every vertical; FS-only sources vanish under a
    # sectorial vertical, so synth never fuses them into that vertical's cards. Non-lens
    # sections (macro/reputation/stores) are untouched. Migrated sources already skip their
    # fetch via _source_enabled; this also gates the still-bespoke FS fetches' OUTPUT.
    _active_ids = {s.id for s in registry.active(_active_vertical())}
    for _sid in [s.id for s in registry.SOURCES]:
        if _sid not in _active_ids:
            payload.pop(_sid, None)

    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if bucket:
        try:
            s3 = boto3.client("s3")
            key = f"lambda-digests/{getattr(context, 'aws_request_id', 'local')}.json"
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        except Exception as exc:  # pragma: no cover - defensive handling for S3 write failures
            print(f"Warning: S3 upload failed: {exc}")

    return {"statusCode": 200, "body": json.dumps(payload, ensure_ascii=False, indent=2)}
