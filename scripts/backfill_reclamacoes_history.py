#!/usr/bin/env python3
"""Backfill the BCB complaints ranking from 2014-S2 (#335).

    AWS_PROFILE=my2027 ONCA_ENTITIES_TABLE=<entities table> \
        .venv/bin/python scripts/backfill_reclamacoes_history.py [--refetch 2] [--dry-run]

Idempotent: a published period's file is kept under ``board_history/reclamacoes/`` and read
from there afterwards; the newest ``--refetch`` periods are fetched again. ``series.json.gz``
is rebuilt each run against the current #330 map and register snapshots. Reads BCB's public
ranking file service. It makes no model calls.
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
from src.ingest import reclamacoes_history as rh  # noqa: E402

BUCKET = "onca-digests-668449743071"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--refetch", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import boto3
    from src.synth import entity_registry as reg
    s3 = boto3.client("s3")
    emap = eh.load_map(s3, args.bucket)
    if not emap or not emap.get("roots"):
        print("no #330 entity map")
        return 1
    names: dict[str, list[str]] = {}

    def resolve(n: str) -> list[str]:
        if n not in names:
            names[n] = reg.resolve_by_name(n)
        return names[n]

    ps = rh.periods()
    fresh = {rh.label(p) for p in ps[-args.refetch:]} if args.refetch else set()
    rows, published, missing = [], [], []
    for p in ps:
        key = f"{rh.PREFIX}{rh.label(p)}.json"
        doc = None if rh.label(p) in fresh else rh.get_json(s3, args.bucket, key)
        if doc is None:
            content = rh.fetch(p)
            time.sleep(0.5)
            if content is None:
                missing.append(rh.label(p))
                continue
            doc = {"schema": rh.SCHEMA, "period": rh.label(p), "rows": rh.parse(content, p)}
            if not args.dry_run:
                rh.put_json(s3, args.bucket, key, doc)
        published.append(rh.label(p))
        q = min(eh.quarter_of(rh.period_end(p)), emap["quarters"][1])
        leaders = rh.conglomerate_leaders(eh.get_snapshot(s3, args.bucket, q) or [])
        rows += rh.attribute(doc["rows"], emap, leaders, resolve)
    s = rh.build_series(rows, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), published, missing)
    print(f"periods: {len(published)} published ({published[0]}..{published[-1]}), not published: {missing}")
    att = sum(1 for r in rows if r.get("entity_id"))
    print(f"rows: {len(rows)}, attributed {att} ({100 * att // max(1, len(rows))}%), "
          f"by name {sum(1 for r in rows if r.get('attribution') == 'name')}; entities {len(s['entities'])}")
    print("top unattributed: " + "; ".join(f"{k} ({v['rows']})" for k, v in list(s["unattributed"].items())[:10]))
    for e in ("itau", "bradesco", "bb", "santander", "caixa", "nubank"):
        pts = rh.series_for(s, e)
        if pts:
            last = pts[-1]
            print(f"  {e}: {len(pts)} periods {pts[0]['period']}..{last['period']}; last index {last['indice']} ({last['method']})")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    rh.put_json(s3, args.bucket, rh.SERIES_KEY, s, gz=True)
    print(f"wrote s3://{args.bucket}/{rh.SERIES_KEY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
