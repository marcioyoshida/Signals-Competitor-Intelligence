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

from src.diff.engine import detect_new
from src.ingest import bcb_normativos, cvm_fundos

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

    digest = {"new_normativos": new_norms, "new_fund_filings": new_funds}
    out = Path(__file__).parent / "data" / "latest_digest.json"
    out.write_text(json.dumps(digest, ensure_ascii=False, indent=2))
    print(f"\nDigest written to {out}")


if __name__ == "__main__":
    main()
