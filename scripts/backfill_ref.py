#!/usr/bin/env python3
"""Load the BCB Financial Stability Report (REF) editions into the KB (#337).

    AWS_PROFILE=my2027 .venv/bin/python scripts/backfill_ref.py [--cache DIR] [--dry-run]
        [--force] [--ingest]

Probes the monthly folders after the newest edition in the manifest (all from 2008-11 on the
first run), cuts each new edition into its sections and writes them to the KB data-source
bucket (``BCB-REF/{edition}/``) with sidecars, then the manifest to the digests bucket.
An edition already in the bucket is skipped; ``--force`` re-cuts every edition, replacing its
objects. ``--ingest`` starts one KB ingestion job when anything was written (Titan embeddings
of the new text only, about $0.05 for the full 2008–2026 set). ``--cache`` reads and keeps
``{YYYYMM}.pdf`` there. Public data; no model calls.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingest import ref_history as rh  # noqa: E402

ACCOUNT = "668449743071"
RAW_BUCKET = f"onca-raw-{ACCOUNT}"
DIGESTS_BUCKET = f"onca-digests-{ACCOUNT}"
KB_ID, DATA_SOURCE_ID = "MHFHPHYIQH", "TJIHHPTRIM"


def pdf(folder: str, pdf_url: str, cache: Path | None) -> bytes:
    if cache and (cache / f"{folder}.pdf").exists():
        return (cache / f"{folder}.pdf").read_bytes()
    content = rh.fetch_pdf(pdf_url)
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"{folder}.pdf").write_bytes(content)
    return content


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--raw-bucket", default=RAW_BUCKET)
    ap.add_argument("--digests-bucket", default=DIGESTS_BUCKET)
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-cut and replace every edition")
    ap.add_argument("--ingest", action="store_true", help="start a KB ingestion job after writing")
    args = ap.parse_args()

    import boto3
    s3 = boto3.client("s3")
    manifest = rh.load_manifest(s3, args.digests_bucket) or {"editions": []}
    known = {e["folder"]: e for e in manifest["editions"]}
    start = rh.FIRST_YM
    if known and not args.force:
        last = max(known)
        start = rh.months(last, "999912")[1]
    today = dt.date.today().strftime("%Y%m")
    found = rh.discover(rh.months(start, today)) if start <= today else {}
    print(f"probed {start}..{today}: {len(found)} new folder(s)")
    targets = {**({f: e["url"] for f, e in known.items()} if args.force else {}), **found}

    entries = {f: e for f, e in known.items()}
    written = 0
    for folder, pdf_url in sorted(targets.items()):
        ed = rh.edition(folder, pdf_url, rh.pdf_pages(pdf(folder, pdf_url, args.cache)))
        stress = [s["label"] for s in ed["sections"] if s["stress"]]
        print(f"{folder} → {ed['edition']}: {ed['pages']} pages, {len(ed['sections'])} sections "
              f"({ed['parse_mode']}), stress: {'; '.join(x[-50:] for x in stress) or 'none'}")
        if not args.dry_run:
            written += rh.write_edition(s3, args.raw_bucket, ed, force=args.force)
        entries[folder] = rh.manifest_entry(ed)
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    if targets:
        doc = rh.put_manifest(s3, args.digests_bucket, entries.values(),
                              dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        print(f"manifest: {len(doc['editions'])} editions → s3://{args.digests_bucket}/{rh.MANIFEST_KEY}")
    print(f"sections written: {written}")
    if written and args.ingest:
        client = boto3.client("bedrock-agent")
        try:
            job = client.start_ingestion_job(knowledgeBaseId=KB_ID, dataSourceId=DATA_SOURCE_ID)
            print(f"ingestion job {job['ingestionJob']['ingestionJobId']} started")
        except client.exceptions.ConflictException:
            print("an ingestion job is already running; the next one will pick these up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
