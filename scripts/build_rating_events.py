#!/usr/bin/env python3
"""Build the rating-action event store (#332) from the CVM IPE history (#331) and the curated
link-only agency pointers (config/rating_pointers.json).

    AWS_PROFILE=my2027 .venv/bin/python scripts/build_rating_events.py [--dry-run]

Reads ``board_history/cvm_ipe/`` and writes ``board_history/rating_events/events.json`` in the
digests bucket. It fetches nothing from the internet: not CVM (its document viewer is behind
a CAPTCHA) and not the agencies (link-only). It makes no model calls. Re-run after each IPE
backfill or pointer edit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingest import cvm_ipe_history as h  # noqa: E402
from src.ingest import rating_events as r  # noqa: E402

BUCKET = "onca-digests-668449743071"
POINTERS = ROOT / "config" / "rating_pointers.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import boto3
    s3 = boto3.client("s3")
    man = json.loads(s3.get_object(Bucket=args.bucket, Key=h.MANIFEST_KEY)["Body"].read())
    rows = []
    for y in sorted(int(k) for k in man["years"]):
        doc = h.get_year(s3, args.bucket, y)
        rows += (doc or {}).get("rows") or []
    pointers = json.loads(POINTERS.read_text(encoding="utf-8"))["pointers"]
    store = r.build(rows, pointers, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    print(f"IPE rows read: {len(rows)}; pointers: {len(pointers)}")
    print(f"events: {store['count']} {store['by_action']}; needs a person to read the filing: "
          f"{store['needs_document']}; unattributed: "
          f"{sum(1 for e in store['events'] if not e.get('entity_id'))}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    r.put(s3, args.bucket, store)
    print(f"wrote s3://{args.bucket}/{r.KEY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
