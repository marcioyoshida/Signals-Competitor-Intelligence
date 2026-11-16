#!/usr/bin/env python3
"""Build the dated insurer ownership map (#389) from SUSEP SES economic groups.

    AWS_PROFILE=my2027 ONCA_ENTITIES_TABLE=<entities table> \
        .venv/bin/python scripts/build_susep_entity_history.py [--dry-run]

Reads one 0.6 MB member of SES's public BaseCompleta.zip through HTTP range requests, SUSEP's
current company list (code -> CNPJ), the registry's CNPJ roots and canonical chains, and the
curated ``config/susep_groups.json``. Writes ``entity_history/susep_map.json.gz``. Idempotent:
the map is rebuilt in full each run, so a registry or group-config fix reaches every quarter.
It makes no model calls.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingest import bcb_entity_history as eh  # noqa: E402
from src.ingest import susep_entidades  # noqa: E402
from src.ingest import susep_entity_history as sh  # noqa: E402

BUCKET = "onca-digests-668449743071"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from src.synth import entity_registry as reg
    rows = sh.parse_groups(sh.fetch_member())
    by_code = sh.code_roots(susep_entidades.fetch_entities())
    m = sh.build_map(rows, by_code, reg.load_cnpj_root_map(), sh.load_group_config(),
                     eh.canonical_map(reg.list_entities(include_inactive=True)))
    codes, roots = m["codes"], m["roots"]
    named = sum(1 for ivs in codes.values() for iv in ivs if iv["group"])
    mapped = sum(1 for ivs in codes.values() for iv in ivs if iv["group"] and iv["group_entity_id"])
    print(f"{len(rows)} rows, quarters {m['quarters']}: {len(codes)} companies, {len(roots)} with a CNPJ root")
    print(f"intervals with a named group: {named}, mapped to a registry entity: {mapped}")
    print(f"companies with a direct registry entity: "
          f"{sum(1 for ivs in codes.values() if any(iv['entity_id'] for iv in ivs))}")
    ch = sh.changes(m)
    print(f"named-group changes: {len(ch)}; latest:")
    for c in ch[-12:]:
        print(f"  {c['at']} {c['name'][:40]:40} {c['from_group']} -> {c['to_group']}"
              f" ({c['from_entity']} -> {c['to_entity']})")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    import boto3
    sh.put_map(boto3.client("s3"), args.bucket, m)
    print(f"wrote s3://{args.bucket}/{sh.MAP_KEY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
