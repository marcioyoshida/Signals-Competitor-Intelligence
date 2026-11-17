#!/usr/bin/env python3
"""Build the SUSEP financial history per insurer (#390) from SES.

    AWS_PROFILE=my2027 .venv/bin/python scripts/backfill_susep_history.py [--dry-run]

Reads three members of SES's public BaseCompleta.zip by HTTP range requests (about 32 MB
compressed in all), attributes every company-quarter through the #389 insurer map
(``entity_history/susep_map.json.gz``, run scripts/build_susep_entity_history.py first) and
writes ``board_history/susep/series.json.gz``. A full rebuild each run: SES republishes the
whole history monthly. It makes no model calls.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingest import susep_entity_history as sh  # noqa: E402
from src.ingest import susep_history as su  # noqa: E402

BUCKET = "onca-digests-668449743071"
SHOW = ("porto_seguro", "bradesco_seguros", "bb_seguridade", "caixa_seguridade", "itauseg",
        "mapfre", "sulamerica", "tokio_marine", "hdi", "irb")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import boto3
    s3 = boto3.client("s3")
    imap = sh.load_map(s3, args.bucket)
    if not imap or not imap.get("codes"):
        print("no #389 insurer map: run scripts/build_susep_entity_history.py first")
        return 1
    raw = {k: sh.fetch_member(m) for k, m in su.MEMBERS.items()}
    print("fetched " + ", ".join(f"{k} {len(v):,} B" for k, v in raw.items()))
    last = su.complete_through(su.months_of(raw["seguros"]))
    print(f"complete through {last}")
    seg = su.parse_seguros(raw["seguros"], last)
    cap = su.parse_stock(raw["capital"], ("pl", "plajustado", "NovoPla", "CMR"), last)
    prov = su.parse_stock(raw["provisoes"], ("valor",), last)
    pts = su.company_points(seg, cap, prov, imap)
    s = su.build_series(pts, su.now(), complete=last, imap_quarters=imap.get("quarters") or [])
    print(f"{s['points']} company-quarters, {s['attributed']} attributed, "
          f"{len(s['companies'])} companies, {len(s['groups'])} groups")
    for e in SHOW:
        g = su.series_for(s, e)
        if not g:
            print(f"  {e}: no series")
            continue
        last_pt = g[-1]
        print(f"  {e}: {g[0]['as_of']}..{last_pt['as_of']} ({len(g)} q); last: earned "
              f"{last_pt['premio_ganho_mi']} mi, loss {last_pt['sinistralidade_pct']}%, "
              f"solvency {last_pt['suficiencia_pct']}%, companies {len(last_pt['companies'])}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    su.put_series(s3, args.bucket, s)
    print(f"wrote s3://{args.bucket}/{su.SERIES_KEY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
