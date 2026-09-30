#!/usr/bin/env python3
"""#159 kill-rule re-measure (~2026-10-27): did the radar surface product changes Onça's
news-driven feed MISSED?

The rule (spike doc §7): if fewer than 3 of the original 5 products produce an unsurfaced,
hand-verified hit in the 30-day window, retire the YouTube creator leg and keep App Store only.
The 5 products added 2026-09-27 are judged separately.

This does the mechanical half and leaves the verification to a human:

1. events = every radar event/alert in the window, from the dated snapshots
   ``product_radar/YYYY-MM-DD.json`` (latest.json alone only holds the recent window);
2. corpus = Onça's narratives ``narratives/<date>/*.json`` from 3 days before to 7 days after
   each event (the spike's baseline);
3. an event is **already in Onça** when a narrative about that product (file name holds one of its
   entity ids, or its text names the brand) shares >= 2 of the event's distinctive words;
   otherwise it is a **candidate unsurfaced hit**.

Output: a markdown worksheet with one checkbox per candidate (verify: real, in-window,
CPO-actionable, not in Onça) and a per-product / per-leg summary.

  AWS_PROFILE=my2027 .venv/bin/python scripts/cpo_radar_remeasure.py --as-of 2026-10-27 --out remeasure.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import cpo_radar as cr  # noqa: E402

BUCKET = "onca-digests-668449743071"
ORIGINAL = ("nubank", "inter", "picpay", "mercado_pago", "c6")   # the spike's 5 (seed file)
# Onça often has a story BEFORE the creators film it (Inter's Priority Pass: narrative 09-22,
# videos 09-27), and earlier coverage means "not unsurfaced": look back two weeks.
BEFORE, AFTER = 14, 7


# words every bank narrative uses: alone they "matched" PicPay's Central de Cashback on
# crédito+limite and BTG's dollar account on "ibovespa em dólar" (dry run 2026-09-30)
_GENERIC = {"credito", "limite", "limites", "oferece", "oferta", "ofertas", "parceria", "parcerias",
            "pelo", "pela", "para", "dolar", "reais", "clientes", "anuncio", "anuncia", "nova", "novas",
            "novos", "sobre", "taxa", "taxas", "servico", "produto", "mercado", "financeiro"}


def event_tokens(ev: dict[str, Any], subject: dict[str, Any]) -> set[str]:
    """Distinctive words (4+ letters, so "pass" in "Priority Pass" counts) of an event's title and
    reason, brand and filler removed."""
    brand = set()
    for a in [subject.get("name", ""), *(subject.get("aliases") or [])]:
        brand |= set(re.findall(r"[a-z0-9]+", cr.fold(a)))
    text = cr.fold(f"{ev.get('title') or ''} {ev.get('reason') or ''}")
    return {t for t in re.findall(r"[a-z0-9]+", text)
            if len(t) >= 4 and t not in cr._STORY_STOP and t not in _GENERIC and t not in brand}


def is_noise(ev: dict[str, Any]) -> bool:
    """An event the CURRENT guards (3ca78dc) would not surface; snapshots before 2026-09-30 hold them."""
    if ev.get("source") != "youtube":
        return False
    m = {"change": ev.get("title"), "why": ev.get("reason"), "title": ev.get("reason")}
    return cr.is_howto(m) or cr.is_corporate(m)


_PROSE_KEYS = {"narrative", "headline", "title", "summary", "text", "reason", "why", "description"}


def prose(raw: str) -> str:
    """Folded PROSE of a narrative file: its narrative/headline/title fields and citation titles.
    Matching on the whole JSON hit metadata (lens names "juros"/"pix", source ids with
    "parcelado") and "matched" Inter's Pix parcelado to a betting narrative."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return cr.fold(raw)
    out: list[str] = []

    def walk(x: Any, key: str = "") -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        elif isinstance(x, str) and key in _PROSE_KEYS:
            out.append(x)
    walk(doc)
    return cr.fold(" ".join(out))


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text))


def _brand_res(subject: dict[str, Any]) -> list[re.Pattern[str]]:
    # whole words: a bare substring "inter" matches "internacional", "interesse"…
    return [re.compile(r"\b%s\b" % re.escape(cr.fold(a)))
            for a in [subject.get("name", ""), *(subject.get("aliases") or [])] if len(cr.fold(a)) >= 3]


def match(ev: dict[str, Any], subject: dict[str, Any], narratives: list[dict[str, Any]]) -> str | None:
    """Pure: the key of the first narrative that already carries this event, else None.
    ``narratives`` rows: {"date", "key", "text"} (text folded)."""
    if ev.get("source") == "appstore":
        return None      # an alert's evidence is its error strings/version, not its generic title
    toks = event_tokens(ev, subject)
    if len(toks) < 2:
        return None                               # too little to match on: leave it to the human
    try:
        d0 = dt.date.fromisoformat(str(ev.get("date"))[:10])
    except ValueError:
        return None
    lo, hi = (d0 - dt.timedelta(days=BEFORE)).isoformat(), (d0 + dt.timedelta(days=AFTER)).isoformat()
    ents = [str(e) for e in subject.get("onca_entities") or []]
    brands = _brand_res(subject)
    for n in narratives:
        if not (lo <= n["date"] <= hi):
            continue
        about = any(e in n["key"] for e in ents) or any(b.search(n["text"]) for b in brands)
        if about and len(toks & n["words"]) >= 2:
            return n["key"]
    return None


def load_events(s3: Any, start: str, end: str) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=cr.PREFIX)
    keys = sorted(o["Key"] for p in pages for o in p.get("Contents", []) if cr._DATED_RE.match(o["Key"]))
    for k in keys:
        snap = json.loads(s3.get_object(Bucket=BUCKET, Key=k)["Body"].read())
        for ev in (snap.get("events") or []) + (snap.get("alerts") or []):
            if start <= str(ev.get("date") or "")[:10] <= end:
                by_id[ev["id"]] = ev              # later snapshots win (more sources merged)
    # a cluster's id follows its first video, so a re-clustered story can reappear under a new
    # id: one row per (product, date, title), keeping the best-corroborated
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for ev in by_id.values():
        k = (str(ev.get("product")), str(ev.get("date")), cr.fold(ev.get("title")))
        if k not in best or (ev.get("n_sources") or 0) > (best[k].get("n_sources") or 0):
            best[k] = ev
    return sorted(best.values(), key=lambda e: (e.get("product") or "", e.get("date") or ""))


def load_narratives(s3: Any, start: str, end: str, cache: Path) -> list[dict[str, Any]]:
    from concurrent.futures import ThreadPoolExecutor

    todo, objs = [], []
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="narratives/")
    for p in pages:
        for o in p.get("Contents", []):
            parts = o["Key"].split("/")
            if len(parts) < 3 or not (start <= parts[1] <= end) or not o["Key"].endswith(".json"):
                continue
            local = cache / o["Key"]
            objs.append((parts[1], o["Key"], local))
            if not local.exists() or local.stat().st_size != o["Size"]:
                todo.append((o["Key"], local))

    def _get(item: tuple[str, Path]) -> None:
        key, local = item
        local.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(BUCKET, key, str(local))

    with ThreadPoolExecutor(max_workers=16) as pool:   # one-by-one took >10 min for ~2k small files
        list(pool.map(_get, todo))
    rows = [{"date": d, "key": k, "text": prose(l.read_text(encoding="utf-8", errors="ignore"))} for d, k, l in objs]
    for r in rows:
        r["words"] = words(r["text"])
    return rows


def worksheet(events: list[dict[str, Any]], subjects: dict[str, dict[str, Any]],
              narratives: list[dict[str, Any]], start: str, end: str) -> tuple[str, dict[str, Any]]:
    rows: dict[str, list[tuple[dict[str, Any], str | None]]] = {}
    for ev in events:
        s = subjects.get(ev.get("product") or "")
        if s and not is_noise(ev):
            rows.setdefault(s["id"], []).append((ev, match(ev, s, narratives)))
    summary: dict[str, Any] = {}
    lines = [f"# CPO radar re-measure — {start} → {end}", "",
             "Tick a candidate only if it is **real, in the window, CPO-actionable, and not in Onça's feed**. "
             "Kill rule: fewer than 3 of the original 5 with a ticked hit → retire the YouTube creator leg.", ""]
    for group, ids in (("Original 5 (the kill rule)", [p for p in ORIGINAL if p in subjects]),
                       ("Added 2026-09-27 (judged separately)", sorted(set(subjects) - set(ORIGINAL)))):
        lines += [f"## {group}", "", "| product | events | already in Onça | candidates (YouTube / App Store) |", "|---|---|---|---|"]
        for pid in ids:
            rs = rows.get(pid, [])
            cand = [e for e, m in rs if not m]
            yt = sum(1 for e in cand if e.get("source") == "youtube")
            summary[pid] = {"events": len(rs), "in_onca": len(rs) - len(cand), "cand_youtube": yt,
                            "cand_appstore": len(cand) - yt, "original": pid in ORIGINAL}
            lines.append(f"| {subjects[pid]['name']} | {len(rs)} | {len(rs) - len(cand)} | {yt} / {len(cand) - yt} |")
        lines.append("")
    for pid in [p for p in ORIGINAL if p in rows] + sorted(set(rows) - set(ORIGINAL)):
        lines += [f"### {subjects[pid]['name']}", ""]
        for ev, m in rows[pid]:
            tag = f"in Onça: `{m}`" if m else "**candidate**"
            box = "- [x]" if m else "- [ ]"
            extra = f", {ev.get('n_channels')} canal(is), {ev.get('confidence')}" if ev.get("source") == "youtube" else ""
            lines.append(f"{box} {ev.get('date')} [{ev.get('type_label') or ev.get('type')}] {ev.get('title')} "
                         f"({ev.get('source')}{extra}) — {ev.get('url') or ''} · {tag}")
        lines.append("")
    return "\n".join(lines), summary


def main(argv: list[str] | None = None) -> int:
    import boto3

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default=str(Path.home() / ".cache" / "onca-remeasure"))
    a = ap.parse_args(argv)
    end = a.as_of
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=a.days - 1)).isoformat()
    s3 = boto3.client("s3")
    subjects = {s["id"]: s for s in cr.load_subjects()}
    events = load_events(s3, start, end)
    n_lo = (dt.date.fromisoformat(start) - dt.timedelta(days=BEFORE)).isoformat()
    n_hi = (dt.date.fromisoformat(end) + dt.timedelta(days=AFTER)).isoformat()
    narratives = load_narratives(s3, n_lo, n_hi, Path(a.cache))
    md, summary = worksheet(events, subjects, narratives, start, end)
    Path(a.out).write_text(md, encoding="utf-8")
    print(json.dumps({"window": [start, end], "events": len(events), "narratives": len(narratives),
                      "products": summary}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
