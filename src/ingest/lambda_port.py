"""Lambda-style prototype for the Phase 1.5 ingestion pipeline.

This keeps the fetch functions pure and exposes a single event handler that
can later be wired to EventBridge + Lambda with minimal changes.

Sources:
  - BCB normativos (regulatory) — detect_new
  - CVM cad_fi funds (competitor) — detect_new
  - BCB IF.data market share — snapshot (no id-diff)
  - BCB autorizações (new entrants) — detect_new, first-run seed suppressed
  - BCB Pix (traction moves) — detect_moves via DynamoDB value state
  - BCB juros médios (pricing moves) — detect_moves via DynamoDB value state
  - CVM ofertas de distribuição — detect_new, first-run seed suppressed
  - SEC EDGAR (US-listed fintechs) — detect_new, first-run seed suppressed
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

from src.diff.engine import DynamoDbState, DynamoDbValueState, detect_moves, detect_new
from src.ingest import (
    bcb_autorizacoes,
    bcb_ifdata,
    bcb_juros,
    bcb_normativos,
    bcb_pix,
    cvm_fundos,
    cvm_ofertas,
    raw_writer,
    sec_filings,
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


def _populate_corpus_and_sync(new_docs: list[dict[str, Any]]) -> None:
    """Write new docs to the raw corpus bucket and trigger a KB ingestion sync.

    A corpus/sync failure must never break the digest response, matching the
    graceful-degradation pattern used for every other external call here.
    """
    raw_bucket = os.environ.get("ONCA_RAW_BUCKET")
    if not raw_bucket:
        return

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


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Return a small digest payload for downstream Lambda/CDK wiring."""
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

    try:
        normativos = bcb_normativos.fetch_recent(days=lookback_days)
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        normativos = []
        print(f"Warning: BCB normativos fetch failed: {exc}")

    try:
        funds = cvm_fundos.fetch_funds(watchlist_admins=competitors)
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        funds = []
        print(f"Warning: CVM funds fetch failed: {exc}")

    try:
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
        authorized = bcb_autorizacoes.fetch_authorized()
        new_entrants = _new_since_last_run(
            "bcb_autorizacoes", authorized, seed_if_empty=True
        )
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        print(f"Warning: BCB autorizações fetch failed: {exc}")

    # Pix traction — month-over-month volume moves (first run seeds baseline only).
    pix_by_inst: list[dict[str, Any]] = []
    pix_moves: list[dict[str, Any]] = []
    try:
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
        offerings = cvm_ofertas.fetch_recent(
            lookback_days=ofertas_lookback,
            watchlist=ofertas_watch or None,
        )
        new_ofertas = _new_since_last_run(
            "cvm_ofertas", offerings, seed_if_empty=True
        )
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        print(f"Warning: CVM ofertas fetch failed: {exc}")

    # SEC EDGAR — US-listed payments/fintech disclosures (seed on first run).
    sec_filings_rows: list[dict[str, Any]] = []
    new_sec: list[dict[str, Any]] = []
    if sec_tickers:
        try:
            sec_filings_rows = sec_filings.fetch_filings(
                sec_tickers, lookback_days=sec_lookback
            )
            new_sec = _new_since_last_run(
                "sec_filings", sec_filings_rows, seed_if_empty=True
            )
        except Exception as exc:  # pragma: no cover
            print(f"Warning: SEC EDGAR fetch failed: {exc}")

    new_normativos = _new_since_last_run("bcb_normativos", normativos)
    new_funds = _new_since_last_run("cvm_fundos", funds)

    # Corpus gets document-like signals only (not numeric Pix/juros moves).
    _populate_corpus_and_sync(
        new_normativos + new_funds + new_entrants + new_ofertas + new_sec
    )

    payload = {
        "regulatory": {
            "count": len(normativos),
            "new_count": len(new_normativos),
            "items": new_normativos[:5],
        },
        "competitor": {
            "count": len(funds),
            "new_count": len(new_funds),
            "items": new_funds[:5],
        },
        "market": {"count": len(market), "items": market},
        "new_entrants": {
            "count": len(authorized),
            "new_count": len(new_entrants),
            "items": new_entrants[:5],
        },
        "pix_moves": {
            "institutions_tracked": len(pix_by_inst),
            "move_count": len(pix_moves),
            "items": pix_moves[:10],
        },
        "juros_moves": {
            "series_tracked": len(juros_focus),
            "move_count": len(juros_moves),
            "items": juros_moves[:10],
        },
        "ofertas": {
            "count": len(offerings),
            "new_count": len(new_ofertas),
            "items": new_ofertas[:10],
        },
        "sec_filings": {
            "count": len(sec_filings_rows),
            "new_count": len(new_sec),
            "items": new_sec[:10],
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
