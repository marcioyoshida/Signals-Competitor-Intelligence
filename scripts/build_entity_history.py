#!/usr/bin/env python3
"""Build the dated CNPJ ↔ entity map (#330) from BCB IF.data's quarterly register.

    AWS_PROFILE=my2027 .venv/bin/python scripts/build_entity_history.py [--dry-run]
        [--start 200003] [--end YYYYMM] [--refetch N]

Incremental and idempotent. Each quarter is fetched once and cached gzipped under
``entity_history/cadastro/`` in the digests bucket (published quarters never change).
The newest ``--refetch`` quarters (default 2) are always fetched again, because BCB
backfills late filers. The map is then rebuilt from the cache against today's registry,
so a registry fix (a new CNPJ root, a merge) reaches every past quarter on the next run.
It reads the registry and writes only ``entity_history/``. It makes no model calls.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingest import bcb_entity_history as eh  # noqa: E402

BUCKET = "onca-digests-668449743071"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--start", type=int, default=eh.FIRST_QUARTER)
    ap.add_argument("--end", type=int, default=None, help="last quarter (default: newest published)")
    ap.add_argument("--refetch", type=int, default=2, help="always re-fetch the newest N quarters")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and report; write nothing to S3 (quarters cache in --cache-dir)")
    ap.add_argument("--cache-dir", default="/var/tmp/onca-entity-history-cache",
                    help="local quarter cache used by --dry-run, so the real run need not refetch")
    args = ap.parse_args()

    import gzip
    import json
    import os

    import boto3

    if not os.environ.get("ONCA_ENTITIES_TABLE"):
        # Same lookup the operators use: the stack's table, not the local-dev default.
        names = boto3.client("dynamodb").list_tables()["TableNames"]
        hits = [n for n in names if n.startswith("OncaPrototypeStack-OncaEntitiesTable")]
        if len(hits) != 1:
            print(f"set ONCA_ENTITIES_TABLE (found {hits})")
            return 1
        os.environ["ONCA_ENTITIES_TABLE"] = hits[0]
    from src.synth import entity_registry as reg

    s3 = boto3.client("s3")
    cache = Path(args.cache_dir)

    def cached(q: int) -> list | None:
        rows = eh.get_snapshot(s3, args.bucket, q)
        if rows is not None:
            return rows
        f = cache / f"{q}.json.gz"
        if not f.exists():
            return None
        rows = json.loads(gzip.decompress(f.read_bytes()))
        if not args.dry_run:
            eh.put_snapshot(s3, args.bucket, q, rows)  # promote a dry run's cache to S3
        return rows

    def keep(q: int, rows: list) -> None:
        if args.dry_run:
            cache.mkdir(parents=True, exist_ok=True)
            (cache / f"{q}.json.gz").write_bytes(gzip.compress(json.dumps(rows).encode()))
        else:
            eh.put_snapshot(s3, args.bucket, q, rows)
    end = args.end or eh.latest_published()
    if not end:
        print("no published quarter found")
        return 1
    qs = eh.quarters(args.start, end)
    fresh = set(qs[-args.refetch:]) if args.refetch else set()

    snaps: dict[int, list] = {}
    fetched = 0
    for q in qs:
        rows = None if q in fresh else cached(q)
        if rows is None:
            rows = eh.fetch_cadastro(q)
            fetched += 1
            if rows:
                keep(q, rows)
            time.sleep(0.5)  # polite to Olinda
        snaps[q] = rows
    print(f"quarters {qs[0]}..{qs[-1]}: {len(qs)} ({fetched} fetched, {len(qs) - fetched} cached)")

    root_map = reg.load_cnpj_root_map()
    canon = eh.canonical_map(reg.list_entities(include_inactive=True))
    m = eh.build_map(snaps, root_map, canon)

    roots = m["roots"]
    ivs = sum(len(v) for v in roots.values())
    mapped = {r for r, v in roots.items() if any(iv.get("group_entity_id") for iv in v)}
    moved = {r for r, v in roots.items()
             if len({iv.get("group_entity_id") for iv in v if iv.get("group_entity_id")}) > 1}
    print(f"roots {len(roots)}, intervals {ivs}, attributed to a registry entity {len(mapped)}, "
          f"changed owner at least once {len(moved)}")
    for probe, as_of in (("01701201", "2015-12-31"), ("01701201", "2016-09-30")):
        print(f"  {probe} @ {as_of}: group={eh.entity_at(m, probe, as_of)} "
              f"direct={eh.entity_at(m, probe, as_of, level='direct')}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    eh.put_map(s3, args.bucket, m)
    print(f"wrote s3://{args.bucket}/{eh.MAP_KEY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
