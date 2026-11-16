#!/usr/bin/env python3
"""Backfill the prudential history (#333): IF.data, COSIF and KM1 per entity with ``as_of``.

    AWS_PROFILE=my2027 .venv/bin/python scripts/backfill_prudential_history.py
        [--sources ifdata,capital,cosif,km1] [--refetch 2] [--dry-run]

Idempotent. A published period never changes, so its extract is fetched once and kept under
``board_history/prudential/``. The newest ``--refetch`` periods of each source are fetched again,
because BCB backfills late filers. KM1 files are re-read every run: the catalog is small and
institutions restate. Then ``series.json.gz`` is rebuilt from the extracts against the current
dated entity map (#330), so a registry fix reaches every past period. Needs the #330 map and its
register cache. Reads public BCB data (Olinda, COSIF files) and the banks' DASFN-registered KM1
files. It makes no model calls.
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
from src.ingest import prudential_history as ph  # noqa: E402

BUCKET = "onca-digests-668449743071"


def months(start: int, end: int) -> list[int]:
    out, y, m = [], divmod(start, 100)[0], start % 100
    while y * 100 + m <= end:
        out.append(y * 100 + m)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--sources", default=",".join(ph.SOURCES))
    ap.add_argument("--refetch", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true", help="fetch and report; write nothing")
    args = ap.parse_args()
    want = set(args.sources.split(","))

    import boto3
    s3 = boto3.client("s3")
    emap = eh.load_map(s3, args.bucket)
    if not emap or not emap.get("roots"):
        print("no #330 entity map: run scripts/build_entity_history.py first")
        return 1
    last_q = emap["quarters"][1]
    snaps: dict[int, list] = {}

    def leaders(q: int, basis: str) -> dict:
        if q not in snaps:
            snaps[q] = eh.get_snapshot(s3, args.bucket, q) or []
        return ph.leader_roots(snaps[q], basis)

    def cached(key: str, fresh: bool):
        return None if fresh else ph.get_json(s3, args.bucket, key)

    def keep(key: str, doc) -> None:
        if not args.dry_run:
            ph.put_json(s3, args.bucket, key, doc)

    points: dict[str, list] = {s: [] for s in ph.SOURCES}

    # ---- IF.data resumo + capital (quarterly) ----
    qs = eh.quarters(ph.FIRST_FINANCEIRO, last_q)
    fresh_q = set(qs[-args.refetch:]) if args.refetch else set()
    for src in ("ifdata", "capital"):
        if src not in want:
            continue
        fetched = 0
        for q in qs:
            if src == "capital" and q < ph.FIRST_CAPITAL:
                continue
            basis = ph.basis_for(q) if src == "ifdata" else "prudencial"
            key = ph.extract_key(src, q, basis if src == "ifdata" else None)
            doc = cached(key, q in fresh_q)
            if doc is None:
                url = ph.resumo_url(q, basis) if src == "ifdata" else ph.sound._capital_url(q)
                try:
                    rows = ph.sound._get(url)
                except Exception as exc:  # one bad quarter must not stop the backfill
                    print(f"  {src} {q}: fetch failed: {str(exc)[:120]}")
                    continue
                if not rows:
                    continue
                doc = {"q": q, "basis": basis, "rows": rows}
                keep(key, doc)
                fetched += 1
                time.sleep(0.5)
            lead = leaders(q, basis)
            pts = (ph.ifdata_points(doc["rows"], q, basis, lead, emap) if src == "ifdata"
                   else ph.capital_points(doc["rows"], q, lead, emap))
            points[src] += pts
        print(f"{src}: {len(points[src])} points ({fetched} quarters fetched)")

    # ---- COSIF (monthly) ----
    if "cosif" in want:
        ms = months(ph.FIRST_COSIF, dt.date.today().year * 100 + dt.date.today().month)
        fresh_m = set(ms[-(args.refetch + 3):]) if args.refetch else set()
        fetched = missing = 0
        for ym in ms:
            key = ph.extract_key("cosif", ym)
            doc = cached(key, ym in fresh_m)
            if doc is None:
                content = ph.fetch_cosif(ym)
                if not content:
                    missing += 1
                    continue
                doc = {"ym": ym, "institutions": ph.parse_cosif(content)}
                keep(key, doc)
                fetched += 1
                time.sleep(0.5)
            points["cosif"] += ph.cosif_points(doc["institutions"], ym, emap)
        print(f"cosif: {len(points['cosif'])} points ({fetched} months fetched, {missing} not published)")

    # ---- KM1 (DASFN catalog, every file) ----
    if "km1" in want:
        from src.ingest import bcb_km1 as k
        cat = k._get(f"{k.DASFN}/Recursos?$filter=Api%20eq%20'pilar3'&$format=json&$top=100000")
        res = [r for r in cat.get("value", []) if (r.get("Recurso") or "").startswith("/km1")
               and r.get("URLDados") and r.get("CnpjInstituicao")]
        raw, failed = [], 0
        for r in res:
            try:
                doc = k._get(r["URLDados"], tries=2, timeout=30)
            except Exception:
                failed += 1
                continue
            bd = k._base_date(r["URLDados"])
            fallback = eh.quarter_of(bd // 100) if bd and 2015_00_00 < bd < 2100_00_00 else None
            raw += ph.km1_points(doc, r["CnpjInstituicao"], r.get("NomeInstituicao") or "",
                                 r["URLDados"], emap, fallback_q=fallback)
            time.sleep(0.3)
        points["km1"] = ph.merge_km1(raw)
        keep(ph.extract_key("km1", "points"), points["km1"])
        print(f"km1: {len(points['km1'])} quarter-points from {len(res)} files ({failed} unreachable)")

    # sources not refreshed this run keep their previous points
    if want != set(ph.SOURCES):
        old = ph.get_json(s3, args.bucket, ph.SERIES_KEY) or {}
        for e, by in (old.get("entities") or {}).items():
            for src, pts in by.items():
                if src not in want:
                    points[src] += pts

    series = ph.build_series(points, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    print(f"series: {len(series['entities'])} entities; attributed points {series['points']}")
    for e in ("bradesco", "itau", "bb", "banrisul"):
        s = ph.series_for(series, e, "ifdata")
        if s:
            print(f"  {e} ifdata {s[0]['as_of']}..{s[-1]['as_of']} ({len(s)} quarters)")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    ph.put_json(s3, args.bucket, ph.SERIES_KEY, series)
    print(f"wrote s3://{args.bucket}/{ph.SERIES_KEY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
