#!/usr/bin/env python3
"""Backfill BCB interest rates per institution and product from 2012 (#334).

    AWS_PROFILE=my2027 .venv/bin/python scripts/backfill_juros_history.py [--refetch-years 1] [--dry-run]

Idempotent. Daily windows are stored one file per year; a past year is fetched once, the
newest ``--refetch-years`` are fetched again (late corrections). Monthly (real estate) and
the Selic target are refetched every run (small). ``series.json.gz`` is rebuilt from the
stored windows against the current #330 dated map. Reads public BCB Olinda and SGS. It makes
no model calls.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingest import bcb_entity_history as eh  # noqa: E402
from src.ingest import juros_history as jh  # noqa: E402

BUCKET = "onca-digests-668449743071"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--refetch-years", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import boto3
    s3 = boto3.client("s3")
    emap = eh.load_map(s3, args.bucket)
    if not emap or not emap.get("roots"):
        print("no #330 entity map: run scripts/build_entity_history.py first")
        return 1
    ch = jh.chain(jh.daily_periods())
    names = jh.names_of(jh.fetch_daily(ch[-1]["inicio"]))      # labels for unattributed roots
    years = sorted({int(w["inicio"][:4]) for w in ch})
    fresh = set(years[-args.refetch_years:]) if args.refetch_years else set()
    windows: list[dict] = []
    for y in years:
        doc = None if y in fresh else jh.get_json(s3, args.bucket, jh.daily_key(y))
        if doc is None:
            ws, failed = [], 0
            for w in (w for w in ch if w["inicio"].startswith(str(y))):
                try:
                    rows = jh.compact(jh.fetch_daily(w["inicio"]))
                except Exception as exc:  # one bad window must not stop the backfill
                    failed += 1
                    print(f"  {w['inicio']}: {str(exc)[:100]}")
                    continue
                ws.append({**w, "rows": rows})
                time.sleep(0.3)
            doc = {"schema": jh.SCHEMA, "year": y, "windows": ws, "failed": failed}
            if not args.dry_run:
                jh.put_json(s3, args.bucket, jh.daily_key(y), doc)
            print(f"{y}: fetched {len(ws)} windows, {sum(len(w['rows']) for w in ws)} rows, {failed} failed")
        else:
            print(f"{y}: stored, {len(doc['windows'])} windows (skipped)")
        windows += doc["windows"]

    monthly = []
    for m in jh.monthly_periods():
        try:
            raw = jh.fetch_monthly(m)
            names.update({k: v for k, v in jh.names_of(raw).items() if k not in names})
            monthly.append({"anomes": m, "rows": jh.compact(raw)})
        except Exception as exc:  # noqa: BLE001
            print(f"  monthly {m}: {str(exc)[:100]}")
        time.sleep(0.2)
    selic = jh.fetch_selic(dt.date(2011, 12, 1), dt.date.today())
    print(f"monthly: {len(monthly)} months; selic: {len(selic)} changes, last {selic[-1] if selic else None}")

    s = jh.build_series(windows, emap, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                        monthly=monthly, names=names)
    print(f"series: {len(s['entities'])} entities, {len(s['market'])} products; rows attributed "
          f"{s['attributed_rows']}, unattributed {s['unattributed_rows']} ({s['unattributed_roots']} roots)")
    top = list(s["unattributed"].items())[:8]
    print("top unattributed: " + "; ".join(f"{k} {v['name'] or '?'} ({v['rows']})" for k, v in top))
    for e in ("itau", "bradesco", "bb", "santander", "caixa", "nubank"):
        ks = jh.products(s, e)
        k = next((k for k in ks if "não consignado" in k), None)
        if k:
            pts = jh.series_for(s, e, k)
            print(f"  {e}: {len(ks)} products; {k}: {pts[0]['as_of']}..{pts[-1]['as_of']} ({len(pts)} pts)")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    jh.put_json(s3, args.bucket, jh.MONTHLY_KEY, monthly)
    jh.put_json(s3, args.bucket, jh.SELIC_KEY, selic)
    jh.put_json(s3, args.bucket, jh.SERIES_KEY, s)
    print(f"wrote s3://{args.bucket}/{jh.SERIES_KEY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
