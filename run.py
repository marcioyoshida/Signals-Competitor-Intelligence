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
from pathlib import Path

import yaml

from src.diff.engine import detect_moves, detect_new
from src.ingest import (
    bcb_autorizacoes,
    bcb_normativos,
    bcb_pix,
    cvm_fundos,
    sec_filings,
)

CONFIG = Path(__file__).parent / "config" / "watchlist.yaml"


def main() -> None:
    cfg = yaml.safe_load(CONFIG.read_text())

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
            filings = sec_filings.fetch_filings(sec_tickers)
            new_sec = detect_new("sec_filings", filings)
            print(f"\nCompetitor — {len(new_sec)} new SEC filings:")
            for f in new_sec:
                print(f"  [{f['filed']}] {f['ticker']} {f['form']}: {f['company']}")
                print(f"     {f['url']}")
        except Exception as e:  # noqa: BLE001 — prototype: isolate source failures
            print(f"\nCompetitor — SEC ingestion skipped ({type(e).__name__}: {e})")
            print("  Set a real User-Agent in sec_filings.HEADERS; verify with "
                  "`python -m src.ingest.sec_filings inspect`.")

    digest = {
        "new_normativos": new_norms,
        "new_fund_filings": new_funds,
        "pix_moves": pix_moves,
        "new_entrants": new_entrants,
        "new_sec_filings": new_sec,
    }
    out = Path(__file__).parent / "data" / "latest_digest.json"
    out.write_text(json.dumps(digest, ensure_ascii=False, indent=2))
    print(f"\nDigest written to {out}")


if __name__ == "__main__":
    main()
