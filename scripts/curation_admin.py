#!/usr/bin/env python3
"""Curation governance admin (ADR 018) — read the mutation journal, roll back a bad
change, and run the integrity audit. Operates on the live registry + OncaCurationLog.

    # audit trail for one entity
    python scripts/curation_admin.py history btg --profile my2027

    # revert a field to its value before a timestamp (a curated write — wins precedence)
    python scripts/curation_admin.py rollback btg industries --before 2026-08-30T18:00:00 --profile my2027

    # undo every rollback-supported change an entity got since a timestamp
    python scripts/curation_admin.py revert-since btg --since 2026-08-30T00:00:00 --profile my2027

    # run the continuous integrity audit against the live registry + published feed
    python scripts/curation_admin.py audit --profile my2027

Requires ONCA_ENTITIES_TABLE + ONCA_CURATION_LOG_TABLE (rollback/history) and, for audit,
ONCA_SITE_BUCKET. --profile sets AWS_PROFILE before boto3.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def cmd_history(args) -> int:
    from src.synth import entity_registry as er

    rows = er.entity_history(args.entity_id, limit=args.limit)
    if not rows:
        print(f"(no journal for {args.entity_id} — is ONCA_CURATION_LOG_TABLE set?)")
        return 1
    for h in rows:  # newest first
        print(f"  {h.get('ts'):40} {str(h.get('action')):18} {str(h.get('source')):10} "
              f"{json.dumps(h.get('detail') or {}, ensure_ascii=False)}")
    return 0


def cmd_rollback(args) -> int:
    from src.synth import entity_registry as er

    try:
        before = er.field_value_before(args.entity_id, args.field, args.before)
        ok = er.rollback_field(args.entity_id, args.field, args.before)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not ok:
        print(f"no prior value for {args.entity_id}.{args.field} before {args.before} — nothing to do")
        return 1
    print(f"OK  {args.entity_id}.{args.field} restored to {before!r} (source=curated)")
    return 0


def cmd_revert_since(args) -> int:
    from src.synth import entity_registry as er

    reverted = er.revert_entity_since(args.entity_id, args.since)
    print(f"reverted fields on {args.entity_id}: {reverted or '(none)'}")
    return 0 if reverted else 1


def cmd_audit(args) -> int:
    import boto3

    from src.synth import entity_registry as er
    from src.synth import integrity

    bucket = os.environ.get("ONCA_SITE_BUCKET")
    feed = {}
    if bucket:
        try:
            feed = json.loads(boto3.client("s3").get_object(
                Bucket=bucket, Key="feed.json")["Body"].read())
        except Exception as exc:  # pragma: no cover
            print(f"(feed.json unavailable: {exc})")
    rep = integrity.audit(feed, list(er.list_entities(include_inactive=True)))
    print(f"integrity: {rep['total']} finding(s) — {rep['counts']}")
    for f in rep["findings"]:
        print(f"  [{f['severity']:>4}] {f['kind']:28} "
              f"{'[safe_fix] ' if f['safe_fix'] else ''}{f['summary']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default=None, help="AWS profile (sets AWS_PROFILE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("history", help="print an entity's mutation journal (newest first)")
    h.add_argument("entity_id")
    h.add_argument("--limit", type=int, default=200)
    h.set_defaults(func=cmd_history)

    r = sub.add_parser("rollback", help="restore a field to its value before a timestamp")
    r.add_argument("entity_id")
    r.add_argument("field", choices=("industries", "parent"))
    r.add_argument("--before", required=True, help="ISO timestamp cutoff")
    r.set_defaults(func=cmd_rollback)

    rs = sub.add_parser("revert-since", help="undo every supported field change since a timestamp")
    rs.add_argument("entity_id")
    rs.add_argument("--since", required=True, help="ISO timestamp")
    rs.set_defaults(func=cmd_revert_since)

    a = sub.add_parser("audit", help="run the integrity audit against live registry + feed")
    a.set_defaults(func=cmd_audit)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
