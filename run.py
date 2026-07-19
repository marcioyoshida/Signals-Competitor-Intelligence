"""Onça data spine — Phase 1 runner.

Produces the milestone output: 'N new BCB normativos this week,
M new CVM fund filings by watchlisted institutions.'

Usage:
    pip install -r requirements.txt
    python run.py

First run seeds state (everything is 'new'); subsequent runs
report only genuine changes. Schedule daily via cron while local;
EventBridge once ported to Lambda.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from src.diff.engine import detect_moves, detect_new
from src.ingest import (
    bcb_autorizacoes,
    bcb_juros,
    bcb_normativos,
    bcb_pix,
    cvm_fundos,
    cvm_ofertas,
    sec_filings,
)

CONFIG = Path(__file__).parent / "config" / "watchlist.yaml"


def main() -> None:
    cfg = yaml.safe_load(CONFIG.read_text())
    if cfg.get("sec_user_agent"):
        os.environ.setdefault("ONCA_SEC_USER_AGENT", str(cfg["sec_user_agent"]))

    print("── Onça digest ──────────────────────────────")

    normativos = bcb_normativos.fetch_recent(days=cfg.get("lookback_days", 7))
    new_norms = detect_new("bcb_normativos", normativos)
    print(f"\nRegulatory — {len(new_norms)} new BCB normativos:")
    for d in new_norms:
        print(f"  [{d['date']}] {d['doc_type']} {d['number']}")
        print(f"     {d['subject'][:100]}")
        print(f"     {d['url']}")

    funds = cvm_fundos.fetch_funds(watchlist_admins=cfg.get("competitors", []))
    new_funds = detect_new("cvm_fundos", funds)
    print(f"\nCompetitor — {len(new_funds)} new CVM fund filings on watchlist:")
    for f in new_funds:
        print(f"  [{f['registered']}] {f['admin'][:40]}")
        print(f"     {f['fund_name']}")

    # Pix traction — month-over-month volume moves for watchlisted institutions.
    # Needs 2+ runs across different competency months to produce signal;
    # the first run just establishes the baseline.
    pix_moves = []
    try:
        pix_rows = bcb_pix.fetch_recent()
        pix_by_inst = bcb_pix.by_institution(
            pix_rows, watchlist_ispb=cfg.get("competitor_ispb", []) or None
        )
        pix_moves = detect_moves(
            "bcb_pix", pix_by_inst,
            key_field="ispb", value_field="tx_value",
            min_pct=cfg.get("pix_move_threshold_pct", 15.0),
        )
        print(f"\nCompetitor — {len(pix_moves)} notable Pix volume moves:")
        for m in pix_moves:
            arrow = "↑" if m["pct_change"] > 0 else "↓"
            print(f"  {arrow} {m['pct_change']:+.1f}%  "
                  f"{(m.get('institution') or m.get('ispb') or '?')[:40]}")
    except Exception as e:  # noqa: BLE001 — prototype: don't let one source break the digest
        print(f"\nCompetitor — Pix ingestion skipped ({type(e).__name__}: {e})")
        print("  Run `python -m src.ingest.bcb_pix inspect` to verify the resource schema.")

    # New entrants — first appearance in the authorized-entities registry.
    # The registry is a full snapshot, so the FIRST run would otherwise flag
    # every institution as 'new'. We detect the seeding run (no prior state)
    # and suppress output, seeding the baseline silently instead.
    new_entrants = []
    try:
        from src.diff.engine import STATE_DIR
        seeded_before = (STATE_DIR / "bcb_autorizacoes.json").exists()
        authorized = bcb_autorizacoes.fetch_authorized()
        detected = detect_new("bcb_autorizacoes", authorized)
        if not seeded_before:
            print(f"\nNew entrants — baseline seeded ({len(authorized)} entities); "
                  f"new authorizations will surface from the next run on.")
        else:
            new_entrants = detected
            print(f"\nNew entrants — {len(new_entrants)} newly authorized entities:")
            for e in new_entrants:
                print(f"  {(e.get('entity_type') or '?')[:30]:30} "
                      f"{(e.get('name') or e.get('cnpj') or '?')[:50]}")
    except Exception as e:  # noqa: BLE001 — prototype: isolate source failures
        print(f"\nNew entrants — Autorizações ingestion skipped ({type(e).__name__}: {e})")
        print("  Run `python -m src.ingest.bcb_autorizacoes inspect` to verify the resource schema.")

    # US-listed Brazilian fintech filings (SEC EDGAR). Only meaningful if
    # payments/acquiring competitors are in scope; controlled by config.
    new_sec = []
    sec_tickers = cfg.get("sec_tickers", [])
    if sec_tickers:
        try:
            filings = sec_filings.fetch_filings(
                sec_tickers,
                lookback_days=int(cfg.get("sec_lookback_days", 365)),
            )
            new_sec = detect_new("sec_filings", filings)
            print(f"\nCompetitor — {len(new_sec)} new SEC filings:")
            for f in new_sec:
                print(f"  [{f['filed']}] {f['ticker']} {f['form']}: {f['company']}")
                print(f"     {f['url']}")
        except Exception as e:  # noqa: BLE001 — prototype: isolate source failures
            print(f"\nCompetitor — SEC ingestion skipped ({type(e).__name__}: {e})")
            print("  Set ONCA_SEC_USER_AGENT / config sec_user_agent; verify with "
                  "`python -m src.ingest.sec_filings inspect`.")

    # Juros médios — rate moves by institution × modality (pricing axis).
    juros_moves = []
    try:
        juros_rows = bcb_juros.fetch_daily()
        modalities = cfg.get("juros_modalities") or []
        if not modalities and cfg.get("juros_use_default_modalities", True):
            modalities = bcb_juros.DEFAULT_MODALITY_FILTERS
        juros_focus = bcb_juros.filter_rates(
            juros_rows,
            institutions=cfg.get("juros_competitors") or None,
            modalities=modalities or None,
        )
        juros_moves = detect_moves(
            "bcb_juros",
            bcb_juros.for_moves(juros_focus),
            key_field="move_key",
            value_field="rate_year",
            min_pct=cfg.get("juros_move_threshold_pct", 10.0),
        )
        print(f"\nCompetitor — {len(juros_moves)} notable juros rate moves "
              f"({len(juros_focus)} series tracked):")
        for m in juros_moves[:20]:
            arrow = "↑" if m["pct_change"] > 0 else "↓"
            print(
                f"  {arrow} {m['pct_change']:+.1f}%  "
                f"{m.get('rate_year')}% a.a.  "
                f"{(m.get('institution') or m.get('cnpj8') or '?')[:30]}  "
                f"{(m.get('modality') or '')[:40]}"
            )
    except Exception as e:  # noqa: BLE001 — prototype: isolate source failures
        print(f"\nCompetitor — Juros médios skipped ({type(e).__name__}: {e})")
        print("  Run `python -m src.ingest.bcb_juros inspect` to verify the API.")

    # CVM ofertas — capital raise / product launch (seed on first run).
    new_ofertas = []
    try:
        from src.diff.engine import STATE_DIR

        ofertas_days = int(cfg.get("ofertas_lookback_days", 30))
        ofertas_watch = list(cfg.get("ofertas_watchlist") or [])
        if not ofertas_watch and cfg.get("ofertas_use_competitors_watchlist", True):
            ofertas_watch = list(cfg.get("competitors") or [])
        seeded_ofertas = (STATE_DIR / "cvm_ofertas.json").exists()
        offerings = cvm_ofertas.fetch_recent(
            lookback_days=ofertas_days,
            watchlist=ofertas_watch or None,
        )
        detected_ofertas = detect_new("cvm_ofertas", offerings)
        if not seeded_ofertas:
            print(
                f"\nCompetitor — CVM ofertas baseline seeded "
                f"({len(offerings)} in last {ofertas_days}d); "
                "new offerings will surface from the next run on."
            )
        else:
            new_ofertas = detected_ofertas
            print(f"\nCompetitor — {len(new_ofertas)} new CVM offerings:")
            for o in new_ofertas[:20]:
                amt = o.get("amount")
                amt_s = f" R$ {amt:,.0f}" if isinstance(amt, (int, float)) else ""
                print(
                    f"  [{o.get('event_date')}] {(o.get('security') or '?')[:28]:28} "
                    f"{(o.get('issuer') or '?')[:32]}{amt_s}"
                )
                if o.get("leader"):
                    print(f"     lead: {o['leader'][:50]}")
    except Exception as e:  # noqa: BLE001 — prototype: isolate source failures
        print(f"\nCompetitor — CVM ofertas skipped ({type(e).__name__}: {e})")
        print("  Run `python -m src.ingest.cvm_ofertas inspect` to verify the dataset.")

    digest = {
        "new_normativos": new_norms,
        "new_fund_filings": new_funds,
        "pix_moves": pix_moves,
        "new_entrants": new_entrants,
        "new_sec_filings": new_sec,
        "juros_moves": juros_moves,
        "new_ofertas": new_ofertas,
    }
    out = Path(__file__).parent / "data" / "latest_digest.json"
    out.write_text(json.dumps(digest, ensure_ascii=False, indent=2))
    print(f"\nDigest written to {out}")


if __name__ == "__main__":
    main()
