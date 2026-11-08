"""One-shot re-clustering of the live sector-events store under the 11-08 join rules.

Dry run (default) reads ``s3://<bucket>/sector_events/latest.json`` read-only, writes the
before/after copies under ``--outdir`` and prints the diff. ``--apply`` first copies the live
object to ``sector_events/backups/latest-<UTC>.json`` (the bucket is NOT versioned), then
publishes. Apply only with the owner's approval and after the code itself is deployed —
otherwise the next synth run keeps clustering with the old rules.

    AWS_PROFILE=my2027 .venv/bin/python -m scripts.sector_events_recluster [--apply]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import sector_events as se  # noqa: E402

BUCKET = "onca-digests-668449743071"


def _row(e):
    return (f"{e['date']} {e['industry']:<12} {e['confidence'][:5]:<5} out={e.get('n_outlets', 0):>3} "
            f"src={len(e.get('sources') or []):>3} {e.get('change_label', ''):<18}"
            f"{' CONTESTED' if e.get('contested') else ''} | {str(e.get('title'))[:80]}")


def diff(before: dict, after: dict, report: dict) -> str:
    out = []
    b = {e["id"]: e for e in before.get("events") or []}
    a = {e["id"]: e for e in after.get("events") or []}
    changed_official = [i for i, e in b.items() if e.get("n_official")
                        and json.dumps(e.get("sources"), sort_keys=True)
                        != json.dumps((a.get(i) or {}).get("sources"), sort_keys=True)]
    out.append(f"official events kept: {report['official_kept']}  changed: {len(changed_official)} {changed_official}")
    out.append(f"press-born events: {report['press_before']} -> {report['press_after']}")
    out.append(f"pending: {len(before.get('pending') or [])} -> {len(after.get('pending') or [])}")
    out.append("\n-- REMOVED (press-born, before) --")
    for i in sorted(set(b) - set(a)):
        out.append(f"  {i}\n     {_row(b[i])}")
    out.append("\n-- RESULTING press-born events (after) --")
    for m in sorted(report["mapping"], key=lambda m: a[m["id"]]["date"], reverse=True):
        e = a[m["id"]]
        tag = "KEPT-ID" if m["id"] in b else "NEW-ID "
        out.append(f"  [{tag}] {m['id']}  <- {', '.join(x.split(':', 1)[1] for x in m['from'])}\n     {_row(e)}")
        if e.get("contested"):
            out.append(f"     rulings={e.get('rulings')}  summary: {e['summary']}")
    out.append(f"\n-- dropped as aftermath of an existing event: {len(report['aftermath'])}; "
               f"rejected: {len(report['rejected'])}")
    from collections import Counter
    out.append("   rejected reasons: " + str(Counter(r["reason"] for r in report["rejected"])))
    out.append("   aftermath of: " + str(Counter(r["event"] for r in report["aftermath"])))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--input", help="local store JSON instead of S3 (offline dry run)")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--outdir", default="/var/tmp/onca-sev")
    ap.add_argument("--apply", action="store_true", help="publish (owner approval required)")
    args = ap.parse_args(argv)
    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(dt.timezone.utc).date()
    if args.input:
        before = json.loads(Path(args.input).read_text(encoding="utf-8"))
    else:
        before = se.load_store(args.bucket)
    if not before.get("events"):
        print("empty store — refusing", file=sys.stderr)
        return 2
    after, report = se.recluster(before, today=today)
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out / f"before-{stamp}.json").write_text(json.dumps(before, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / f"after-{stamp}.json").write_text(json.dumps(after, ensure_ascii=False, indent=1), encoding="utf-8")
    print(diff(before, after, report))
    print(f"\nwrote {out}/before-{stamp}.json and after-{stamp}.json")
    if not args.apply:
        print("DRY RUN — nothing written to S3.")
        return 0
    import boto3
    s3 = boto3.client("s3")
    s3.copy_object(Bucket=args.bucket, Key=f"sector_events/backups/latest-{stamp}.json",
                   CopySource={"Bucket": args.bucket, "Key": se.STORE_KEY})
    print("published", se.publish(after, args.bucket, s3=s3),
          f"(backup sector_events/backups/latest-{stamp}.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
