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
    bcb_normativos,
    bcb_pix,
    cvm_fundos,
    cvm_inf_diario,
    cvm_ipe,
    cvm_ofertas,
    dou,
    raw_writer,
    receita_cnpj,
    sec_filings,
    trade_press,
)


def _new_since_last_run(
    source: str,
    docs: list[dict[str, Any]],
    *,
    seed_if_empty: bool = False,
) -> list[dict[str, Any]]:
    """Diff docs against DynamoDB-backed state; degrade gracefully on failure.

    When seed_if_empty is True (autorizações registry), the first run with
    an empty state table seeds the baseline and reports nothing — otherwise
    every authorized institution would appear as a "new entrant".
    """
    try:
        state = DynamoDbState(source)
        if hasattr(state, "load"):
            state.load()
        was_empty = len(state.seen) == 0
        fresh = detect_new(source, docs, state=state)
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
                new_news = _new_since_last_run("trade_press", news_items, seed_if_empty=True)
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
    }


def _empty_news_slice() -> dict[str, Any]:
    return {"count": 0, "new_count": 0, "items": [], "context": []}


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

    try:
        with _source_budget("BCB normativos", deadline, per_source):
            normativos = bcb_normativos.fetch_recent(days=lookback_days)
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        normativos = []
        print(f"Warning: BCB normativos fetch failed: {exc}")

    try:
        with _source_budget("CVM funds", deadline, per_source):
            funds = cvm_fundos.fetch_funds(watchlist_admins=competitors)
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        funds = []
        print(f"Warning: CVM funds fetch failed: {exc}")

    try:
        with _source_budget("IF.data market", deadline, per_source):
            base_date = bcb_ifdata.latest_base_date()
            rows = bcb_ifdata.fetch_institutions(base_date=base_date)
            names = bcb_ifdata.fetch_institution_names(base_date)
            market = bcb_ifdata.market_share(rows, institution_names=names)[:10]
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

    # CVM ofertas — capital raise / product launch (seed suppressed on first run).
    offerings: list[dict[str, Any]] = []
    new_ofertas: list[dict[str, Any]] = []
    try:
        with _source_budget("CVM ofertas", deadline, per_source):
            offerings = cvm_ofertas.fetch_recent(
                lookback_days=ofertas_lookback,
                watchlist=ofertas_watch or None,
            )
            new_ofertas = _new_since_last_run(
                "cvm_ofertas", offerings, seed_if_empty=True
            )
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        print(f"Warning: CVM ofertas fetch failed: {exc}")

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

    # Diário Oficial — official acts (SUSEP/CADE/BACEN) naming a competitor.
    dou_acts: list[dict[str, Any]] = []
    new_dou: list[dict[str, Any]] = []
    if dou_terms:
        try:
            with _source_budget("Diário Oficial", deadline, per_source):
                dou_acts = dou.fetch_dou(dou_terms, lookback_days=dou_lookback)
                new_dou = _new_since_last_run("dou", dou_acts, seed_if_empty=True)
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: Diário Oficial fetch failed: {exc}")

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

    new_normativos: list[dict[str, Any]] = []
    new_funds: list[dict[str, Any]] = []
    try:
        with _source_budget("state diffs", deadline, per_source):
            new_normativos = _new_since_last_run("bcb_normativos", normativos)
            new_funds = _new_since_last_run("cvm_fundos", funds)
    except Exception as exc:  # pragma: no cover - deadline reached / state unavailable
        print(f"Warning: normativos/funds diff skipped: {exc}")

    # Corpus gets document-like signals only (not numeric Pix/juros/AUM moves).
    _populate_corpus_and_sync(
        new_normativos + new_funds + new_entrants + new_ofertas + new_sec
        + new_fatos + new_dou
    )

    # Stage B fusion needs more than deltas: after seeding, items[] is often
    # empty while pulls still succeed. Attach compact context samples and
    # tag delta rows with is_new for prioritization.
    payload = {
        "regulatory": {
            "count": len(normativos),
            "new_count": len(new_normativos),
            "items": _tag_new(new_normativos[:8]),
            "context": _strip_raw(normativos[:12]),
        },
        "competitor": {
            "count": len(funds),
            "new_count": len(new_funds),
            "items": _tag_new(new_funds[:8]),
            "context": _strip_raw(funds[:12]),
        },
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
        "ofertas": {
            "count": len(offerings),
            "new_count": len(new_ofertas),
            "items": _tag_new(new_ofertas[:10]),
            "context": _strip_raw(offerings[:15]),
        },
        "sec_filings": {
            "count": len(sec_filings_rows),
            "new_count": len(new_sec),
            "items": _tag_new(new_sec[:10]),
            "context": _strip_raw(sec_filings_rows[:15]),
        },
        "fatos": {
            "count": len(fatos),
            "new_count": len(new_fatos),
            "governance_count": sum(1 for f in new_fatos if f.get("governance")),
            "items": _tag_new(new_fatos[:12]),
            "context": _strip_raw(fatos[:15]),
        },
        "dou": {
            "count": len(dou_acts),
            "new_count": len(new_dou),
            "items": _tag_new(new_dou[:10]),
            "context": _strip_raw(dou_acts[:15]),
        },
        # In "all" mode news is fetched inline here; in "structured" mode the news
        # slice is empty and the parallel news branch supplies it (overlaid at synth).
        "news": _news_slice(context) if mode == "all" else _empty_news_slice(),
        "inf_diario_moves": {
            "funds_tracked": len(inf_diario_rows),
            "as_of": (inf_diario_rows[0].get("date") if inf_diario_rows else None),
            "move_count": len(inf_diario_moves),
            "items": _tag_new(inf_diario_moves[:10]),
            # Top-by-PL sample for entity fusion (already sorted in fetch_latest).
            "context": _strip_raw(inf_diario_rows[:15]),
        },
        "source": "lambda_port",
    }

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
