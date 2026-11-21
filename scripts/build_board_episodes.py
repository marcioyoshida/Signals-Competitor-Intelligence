#!/usr/bin/env python3
"""Build the historic board episodes (#339) from the M1 stores.

    AWS_PROFILE=my2027 ONCA_ENTITIES_TABLE=<entities table> \
        .venv/bin/python scripts/build_board_episodes.py [--only ID] [--dry-run]

Reads the digests bucket (prudential, IPE, rating events, Selic, complaints, B3, REF
manifest) and the REF stress sections from the KB bucket, then writes one
``eval/board/episodes/<id>.json`` per episode plus ``index.json``. Failed banks get their
IF.data points from the stored extracts and their B3 rows from the year files (no registry
write). Read-only on AWS; no model calls. Replay one with
``scripts/board_dry_run.py --scenario eval/board/episodes/<id>.json``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.board import episodes as epm  # noqa: E402
from src.ingest import bcb_entity_history as eh  # noqa: E402
from src.ingest import cotahist_history as ch  # noqa: E402
from src.ingest import cvm_ipe_history as ipe  # noqa: E402
from src.ingest import juros_history as jh  # noqa: E402
from src.ingest import prudential_history as ph  # noqa: E402
from src.ingest import reclamacoes_history as rc  # noqa: E402
from src.ingest import ref_history as rh  # noqa: E402

DIGESTS = "onca-digests-668449743071"
RAW = "onca-raw-668449743071"


def failed_points(s3, emap, roots: dict[str, str], quarters: list[int]) -> dict[str, dict[str, list]]:
    """{episode entity: {"ifdata": [points]}} from the stored IF.data extracts: per quarter,
    the conglomerate row led by the root, else the institution's own row."""
    out: dict[str, dict[str, list]] = {eid: {"ifdata": []} for eid in roots.values()}
    for q in quarters:
        basis = ph.basis_for(q)
        doc = ph.get_json(s3, DIGESTS, ph.extract_key("ifdata", q, basis))
        if not doc:
            continue
        leaders = ph.leader_roots(eh.get_snapshot(s3, DIGESTS, q) or [], basis)
        best: dict[str, dict] = {}
        for p in ph.ifdata_points(doc["rows"], q, basis, leaders, emap):
            eid = roots.get(p.get("root") or "")
            if eid and (eid not in best or (p["conglomerate"] and not best[eid]["conglomerate"])):
                best[eid] = p
        for eid, p in best.items():
            out[eid]["ifdata"].append({**p, "entity_id": eid})
    return out


def failed_b3(s3, series, roots: dict[str, str], years: set[int]) -> None:
    """Add the failed banks' B3 rows (unattributed in the series) under their episode ids."""
    want = {}
    for t, info in (series.get("tickers") or {}).items():
        if info.get("cnpj8") in roots:
            want[info["issuer"]] = info["cnpj8"]
    if not want:
        return
    rows = []
    for y in sorted(years):
        doc = ch.get_json(s3, DIGESTS, ch.year_key(y)) or {}
        rows += [r for r in doc.get("rows") or [] if r["issuer"] in want]
    mini = ch.build_series(rows, want, {"roots": {}}, roots, "episode")
    for eid, tickers in mini["entities"].items():
        series.setdefault("entities", {})[eid] = tickers


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import boto3
    from src.synth import entity_registry as reg
    s3 = boto3.client("s3")
    cfg = epm.load_config()
    eps = [e for e in cfg["episodes"] if not args.only or e["id"] == args.only]

    emap = eh.load_map(s3, DIGESTS)
    labels = {e["entity_id"]: e.get("display_name") or e.get("news_term") or e["entity_id"]
              for e in reg.list_entities(include_inactive=True)}
    years = {int(e[k][:4]) + d for e in eps for k in ("as_of", "event_date") for d in (-1, 0, 1)}
    ipe_rows = []
    for y in sorted(y for y in years if y >= ipe.FIRST_YEAR):
        ipe_rows += (ipe.get_year(s3, DIGESTS, y) or {}).get("rows") or []
    ev = json.loads(s3.get_object(Bucket=DIGESTS, Key="board_history/rating_events/events.json")["Body"].read())
    b3 = ch.get_json(s3, DIGESTS, ch.SERIES_KEY)
    roots = {v["cnpj8"]: k for e in eps for k, v in (e.get("episode_entities") or {}).items()}
    quarters = sorted({q for e in eps if e.get("episode_entities")
                       for q in epm.next_quarters(epm.visible_quarter(e["as_of"]) - 100, 7)})  # q0 ± 3
    failed_b3(s3, b3, roots, years)
    manifest = rh.load_manifest(s3, DIGESTS)
    ref_text = {}
    for e in eps:
        st = rh.stress_at(manifest, e["as_of"])
        if st and st["sections"]:
            k = st["sections"][0]["key"]
            ref_text[k] = s3.get_object(Bucket=RAW, Key=k)["Body"].read().decode("utf-8")
    stores = {
        "prudential": ph.get_json(s3, DIGESTS, ph.SERIES_KEY),
        "episode_points": failed_points(s3, emap, roots, quarters) if roots else {},
        "labels": labels, "ipe_rows": ipe_rows, "rating_events": ev.get("events") or [],
        "selic": jh.get_json(s3, DIGESTS, jh.SELIC_KEY) or [], "complaints": rc.get_json(s3, DIGESTS, rc.SERIES_KEY),
        "b3": b3, "ref_manifest": manifest, "ref_text": ref_text,
    }
    built = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    index = []
    for e in eps:
        doc = epm.episode_doc(e, stores, cfg["rubrics"], built)
        key = doc["answer_key"]
        line = (f"{e['id']}: usable={doc['usable']} ({doc['usable_reason']}); "
                f"{len(doc['feed']['entities'])} entities, {len(doc['feed']['feed'])} feed items, "
                f"{doc['coverage']['sector_events']} sector events; decisions {key['decision_categories']}; "
                f"market {({k: v['abnormal_pct'] for k, v in key['market_reaction'].items()})}")
        if "pre_event_signal" in key:
            line += f"; pre-event signal {key['pre_event_signal']}"
        print(line)
        index.append({"id": e["id"], "kind": e["kind"], "as_of": e["as_of"], "event_date": e["event_date"],
                      "focal": e["focal"], "usable": doc["usable"], "usable_reason": doc["usable_reason"]})
        if not args.dry_run:
            (epm.OUT_DIR / f"{e['id']}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n",
                                                          encoding="utf-8")
    if not args.dry_run and not args.only:
        (epm.OUT_DIR / "index.json").write_text(json.dumps(
            {"schema": "board/v1/episode-index", "config_version": cfg["version"], "built_at": built,
             "usable": sum(1 for i in index if i["usable"]), "episodes": index, "excluded": cfg.get("excluded") or []},
            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{sum(1 for i in index if i['usable'])}/{len(index)} usable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
