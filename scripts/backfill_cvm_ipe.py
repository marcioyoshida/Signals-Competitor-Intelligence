#!/usr/bin/env python3
"""Backfill CVM IPE for financial institutions and insurers from 2014 (#331, #388): material facts + board minutes.

    AWS_PROFILE=my2027 .venv/bin/python scripts/backfill_cvm_ipe.py [--dry-run]
        [--start 2014] [--end YYYY] [--refetch 2]

Idempotent. A past year's file is immutable, so it is skipped once stored. The newest
``--refetch`` years (default 2) are always rebuilt, because CVM keeps adding late filings and
new versions. The filter and attribution come from the dated entity map (#330), which must
already be in the bucket. Reads CVM's public ZIPs, writes only ``board_history/cvm_ipe/``. It
makes no model calls.
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
from src.ingest import cvm_ipe  # noqa: E402
from src.ingest import cvm_ipe_history as h  # noqa: E402

BUCKET = "onca-digests-668449743071"


def insurers() -> dict:
    """#388: registry ``insurance`` roots plus SUSEP's supervised-company list."""
    from src.ingest import susep_entidades
    from src.synth import entity_registry as reg
    roots = reg.load_cnpj_root_map()
    ins = {e for e, v in reg.list_entity_attributes().items()
           if "insurance" in (v.get("industries") or [])}
    try:
        susep = [(r.get("cnpj") or "")[:8] for r in susep_entidades.fetch_entities()]
    except Exception as exc:  # the registry roots still cover the listed insurers
        print(f"SUSEP list unavailable: {str(exc)[:100]}")
        susep = []
    return h.insurer_roots(roots, ins, susep)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--start", type=int, default=h.FIRST_YEAR)
    ap.add_argument("--end", type=int, default=dt.date.today().year)
    ap.add_argument("--refetch", type=int, default=2, help="always rebuild the newest N years")
    ap.add_argument("--banks-only", action="store_true", help="skip the #388 insurer roots")
    ap.add_argument("--dry-run", action="store_true", help="build and report; write nothing")
    args = ap.parse_args()

    import boto3
    s3 = boto3.client("s3")
    emap = eh.load_map(s3, args.bucket)
    if not emap or not emap.get("roots"):
        print(f"no entity map at s3://{args.bucket}/{eh.MAP_KEY}: run scripts/build_entity_history.py first")
        return 1
    print(f"entity map: {len(h.fi_roots(emap))} CNPJ roots")
    extra = {} if args.banks_only else insurers()
    from src.ingest import susep_entity_history as sh
    imap = None if args.banks_only else sh.load_map(s3, args.bucket)
    print(f"insurer map (#389): {len((imap or {}).get('roots') or {})} dated roots")
    print(f"insurer roots outside the map: {sum(1 for r in extra if r not in h.fi_roots(emap))}")

    years = list(range(args.start, args.end + 1))
    fresh = set(years[-args.refetch:]) if args.refetch else set()
    summary: dict[int, dict] = {}
    for y in years:
        if y not in fresh:
            old = h.get_year(s3, args.bucket, y)
            if old:
                summary[y] = {k: v for k, v in old.items() if k != "rows"}
                print(f"{y}: stored, {old['records']} records (skipped)")
                continue
        content = cvm_ipe._fetch_year_zip(y)
        if not content:
            print(f"{y}: no ZIP published")
            continue
        rows = h.select(h.year_records(content), emap, extra, imap)
        s = h.summarize(rows)
        print(f"{y}: {len(content):,} B → {s['records']} records from {s['filers']} filers, "
              f"{s['attributed']} attributed; {s['by_class']}")
        if not args.dry_run:
            summary[y] = h.put_year(s3, args.bucket, y, rows, source_bytes=len(content))
        time.sleep(1)  # polite to CVM
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    if summary:
        h.put_manifest(s3, args.bucket, summary, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        print(f"wrote s3://{args.bucket}/{h.MANIFEST_KEY} ({len(summary)} years)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
