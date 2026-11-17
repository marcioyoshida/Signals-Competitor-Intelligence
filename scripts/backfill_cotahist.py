#!/usr/bin/env python3
"""Backfill B3 daily prices for listed banks, insurers and holdings from 2000 (#336).

    AWS_PROFILE=my2027 ONCA_ENTITIES_TABLE=<entities table> \
        .venv/bin/python scripts/backfill_cotahist.py [--refetch 1] [--cache DIR] [--dry-run]

Idempotent: each past year's in-scope rows are stored once under ``board_history/b3/``; the
newest ``--refetch`` years are fetched again. CVM FCA years are fetched with CVM's 10-second
crawl delay. ``series.json.gz`` is rebuilt every run against the current #330 map, so a map
or registry fix reaches every past day. ``--cache`` reuses ZIPs already on disk. Public data
only; no model calls.
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
from src.ingest import cotahist_history as ch  # noqa: E402

BUCKET = "onca-digests-668449743071"


def fetch(url: str, cache: Path | None, name: str, delay: float = 1.0) -> bytes | None:
    import requests
    if cache and (cache / name).exists():
        return (cache / name).read_bytes()
    r = requests.get(url, timeout=900)
    time.sleep(delay)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--refetch", type=int, default=1)
    ap.add_argument("--cache", type=Path, default=None, help="dir with COTAHIST_A*.ZIP / fca_*.zip")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import boto3
    from src.synth import entity_registry as reg
    s3 = boto3.client("s3")
    emap = eh.load_map(s3, args.bucket)
    if not emap or not emap.get("roots"):
        print("no #330 entity map")
        return 1
    reg_roots = reg.load_cnpj_root_map()
    this_year = dt.date.today().year

    pairs: dict[str, set[str]] = {}
    for y in range(ch.FIRST_FCA_YEAR, this_year + 1):
        content = fetch(ch.FCA_URL.format(year=y), args.cache, f"fca_{y}.zip", delay=10.0)  # CVM crawl delay
        for code, roots in ch.fca_pairs(content or b"PK\x05\x06" + b"\0" * 18).items():
            pairs.setdefault(code, set()).update(roots)
    scope = set(emap["roots"]) | set(reg_roots)
    codes = ch.issuers(pairs, ch.load_supplement(), scope)
    print(f"issuer codes: {len(pairs)} from FCA, {len(codes)} in scope")

    rows = []
    fresh = set(range(this_year - args.refetch + 1, this_year + 1)) if args.refetch else set()
    for y in range(ch.FIRST_YEAR, this_year + 1):
        doc = None if y in fresh else ch.get_json(s3, args.bucket, ch.year_key(y))
        if doc is None or set(doc.get("codes") or []) != set(codes):
            content = fetch(ch.URL.format(year=y), args.cache, f"COTAHIST_A{y}.ZIP")
            if not content:
                print(f"{y}: no file")
                continue
            doc = {"schema": ch.SCHEMA, "year": y, "codes": sorted(codes), "rows": ch.parse_year(content, codes)}
            if not args.dry_run:
                ch.put_json(s3, args.bucket, ch.year_key(y), doc)
            print(f"{y}: {len(doc['rows'])} rows")
        rows += doc["rows"]
    s = ch.build_series(rows, codes, emap, reg_roots, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    flagged = sum(1 for e in s["entities"].values() for pts in e.values() for p in pts if p[7])
    print(f"series: {len(s['tickers'])} tickers, {len(s['entities'])} entities; "
          f"{sum(s['unattributed_days'].values())} unattributed ticker-days; {flagged} flagged jumps")
    print("unattributed: " + ", ".join(f"{t} ({n})" for t, n in sorted(s["unattributed_days"].items(), key=lambda kv: -kv[1])[:12]))
    for e in ("itau", "bradesco", "bb", "santander", "btg", "unibanco", "bb_seguridade", "porto_seguro"):
        ts = (s["entities"].get(e) or {})
        if ts:
            print(f"  {e}: " + ", ".join(f"{t} {p[0][0]}..{p[-1][0]}" for t, p in sorted(ts.items())[:6]))
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    ch.put_json(s3, args.bucket, ch.SERIES_KEY, s)
    print(f"wrote s3://{args.bucket}/{ch.SERIES_KEY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
