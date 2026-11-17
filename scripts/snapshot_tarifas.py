#!/usr/bin/env python3
"""Take a dated snapshot of the BCB bank-fee tables (#335).

    AWS_PROFILE=my2027 .venv/bin/python scripts/snapshot_tarifas.py [--dry-run]

Olinda serves only the fees in force today, so history is one snapshot per run (about 1,200
calls, ~10 min). Writes ``board_history/tarifas/{date}.json.gz`` and prints the changes since
the previous snapshot. Run it monthly. It makes no model calls.
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingest import bcb_entity_history as eh  # noqa: E402
from src.ingest import tarifas_history as th  # noqa: E402

BUCKET = "onca-digests-668449743071"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import boto3
    s3 = boto3.client("s3")
    emap = eh.load_map(s3, args.bucket) or {"quarters": [], "roots": {}}
    day = th.today()
    snap = th.snapshot(day, emap, log=print)
    insts = snap["institutions"]
    with_fees = sum(1 for i in insts.values() if i.get("F") or i.get("J"))
    print(f"{day}: {len(insts)} institutions, {with_fees} with a fee table, {snap['failed']} failed calls, "
          f"{sum(1 for i in insts.values() if i.get('entity_id'))} attributed")
    since = collections.Counter(f["since"][:4] for i in insts.values() for c in th.CLIENT_TYPES
                                for f in (i.get(c) or []) if f.get("since"))
    print("current fees by year in force since: " + ", ".join(f"{y}:{n}" for y, n in sorted(since.items())))
    prev_key = th.latest_snapshot_key(s3, args.bucket, before=day)
    if prev_key:
        ch = th.changes(th.get_json(s3, args.bucket, prev_key), snap)
        print(f"changes since {prev_key}: {len(ch)}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    th.put_json(s3, args.bucket, th.snapshot_key(day), snap)
    print(f"wrote s3://{args.bucket}/{th.snapshot_key(day)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
