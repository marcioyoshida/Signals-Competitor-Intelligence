"""Pilot — ingest international banking-regulatory updates (Basel Committee / BIS) and
normalize them into the reg-doc shape the existing reg-change machinery consumes.

Feasibility (probed live, 2026-09-06):
  - BIS blocks bot UAs (403) and has NO dedicated BCBS-publications RSS; the pragmatic monitor
    is the **BIS press-release feed** `/doclist/all_pressrels.rss` (carries "Basel Committee ..."
    announcements) plus the **FSI publications feed** `/doclist/bis_fsi_publs.rss` (FSI Insights on
    Basel implementation). Both 200 with a browser-like UA; RSS 1.0 / RDF (dc:date, not pubDate);
    rolling ~10-item window → the seen-set / content-hash pattern handles recurrence.

This mirrors `src/ingest/bcb_normativos.py` (fetch → normalize) but for the UPSTREAM international
tier: a BCBS standard/update is what the domestic CMN/BCB act later transposes. Output records are
shaped like the reg-doc/normativo docs so they slot into reg_change / reg_documents / reg_coverage.

Run:  .venv/bin/python scripts/pilot_bis_bcbs.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys

import requests

FEEDS = {
    "bis_press": "https://www.bis.org/doclist/all_pressrels.rss",
    "bis_fsi":   "https://www.bis.org/doclist/bis_fsi_publs.rss",
}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# Banking-regulation relevance gate (English source). Two-part, auditable:
#   STRONG  — an unambiguous banking-regulation trigger admits on its own.
#   WEAK    — generic prudential terms admit ONLY alongside a banking-regulation context word
#             (the pilot showed bare "liquidity"/"reserve" let unrelated working papers through).
STRONG = ("basel", "bcbs", "committee on banking supervision", "g-sib", "d-sib",
          "lcr", "nsfr", "leverage ratio", "capital framework", "prudential standard",
          "operational resilience", "ict risk", "basel iii", "basel framework")
# NB bare "liquidity" is deliberately NOT weak — it admits monetary-ops/reserve-demand papers;
# prudential liquidity is caught by LCR/NSFR in STRONG.
WEAK = ("capital", "prudential", "supervis", "rwa", "solvency", "crypto")
CONTEXT = ("bank", "basel", "bcbs", "prudential", "supervis")


def _fetch(url: str) -> str:
    r = requests.get(url, timeout=30, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "application/xml,text/xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    r.raise_for_status()
    return r.text


def _tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


def parse_feed(source: str, xml: str) -> list[dict]:
    items = re.findall(r"<item[ >].*?</item>", xml, re.S) or re.findall(r"<entry[ >].*?</entry>", xml, re.S)
    out = []
    for it in items:
        title = _tag(it, "title")
        link = _tag(it, "link") or (re.search(r'rdf:about="([^"]+)"', it) or [None, None])[1] if 'rdf:about' in it else _tag(it, "link")
        date = _tag(it, "dc:date") or _tag(it, "pubDate") or _tag(it, "date")
        desc = _tag(it, "description") or _tag(it, "content:encoded")
        out.append({"source": source, "title": title, "url": link, "date": date[:10], "summary": desc})
    return out


def relevant(rec: dict) -> bool:
    hay = f"{rec['title']} {rec['summary']}".lower()
    if any(k in hay for k in STRONG):
        return True
    # weak terms need a banking-regulation context word in the same item
    return any(w in hay for w in WEAK) and any(c in hay for c in CONTEXT)


def normalize(rec: dict) -> dict:
    """Reg-doc-shaped record, tagged as the international/upstream regulatory tier."""
    rid = "bis:" + hashlib.sha1((rec["url"] or rec["title"]).encode("utf-8")).hexdigest()[:12]
    return {
        "id": rid,
        "source": "BIS/BCBS",
        "tier": "international",              # upstream of the domestic CMN/BCB transposition
        "kind": "intl_reg_update",
        "lens": "regulatory",
        "title": rec["title"],
        "url": rec["url"],
        "date": rec["date"],
        "summary": (rec["summary"] or "")[:500],
        "feed": rec["source"],
    }


def main() -> None:
    all_records: list[dict] = []
    for source, url in FEEDS.items():
        try:
            recs = parse_feed(source, _fetch(url))
        except Exception as e:  # noqa: BLE001
            print(f"  {source}: FETCH FAILED — {e}", file=sys.stderr)
            continue
        hits = [normalize(r) for r in recs if relevant(r)]
        print(f"  {source}: {len(recs)} items, {len(hits)} banking-regulation relevant", file=sys.stderr)
        all_records.extend(hits)

    with open("/tmp/pilot_bis_bcbs.json", "w", encoding="utf-8") as fh:
        json.dump({"count": len(all_records), "records": all_records}, fh, ensure_ascii=False, indent=2)

    print(f"\n{len(all_records)} international banking-regulatory update(s):")
    for r in all_records:
        print(f"  [{r['date'] or '?'}] {r['title'][:88]}")
        print(f"           {r['url']}")
    print("\nwrote /tmp/pilot_bis_bcbs.json")


if __name__ == "__main__":
    main()
