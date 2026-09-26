"""Yield table + per-product baseline (#158 spike). Deterministic, no model calls.

Bluefin baseline.py/series.py pattern: record counts, then measure deviation against the subject's
OWN normal. Here: daily counts per (product, event), mean/sd over the 30-day window (zero days are
data), and spikes = days with count >= MIN_SPIKE and z >= Z. Output: out/yield.json, out/spikes.json.
"""
from __future__ import annotations

import collections
import datetime as dt
import statistics

import common as c

Z, MIN_SPIKE = 2.0, 3
CHANGE_EVENTS = {"launch", "feature", "price", "outage"}


def days() -> list[str]:
    d0 = dt.date.fromisoformat(c.WINDOW_START)
    return [(d0 + dt.timedelta(n)).isoformat() for n in range(30)]


def main() -> None:
    ms = c.load("classified.json")
    fees = c.load("bcb_fees.json")
    yt, rows = {}, []
    for pid in [s["id"] for s in c.subjects()]:
        for src in ("youtube", "appstore"):
            sub = [m for m in ms if m["product"] == pid and m["source"] == src]
            rel = [m for m in sub if m.get("relevant")]
            ev = collections.Counter(m["event"] for m in rel)
            rows.append({"product": pid, "source": src, "mentions": len(sub), "relevant": len(rel),
                         "relevant_pct": round(100 * len(rel) / len(sub), 1) if sub else None,
                         "change_events": sum(ev[e] for e in CHANGE_EVENTS),
                         "by_event": dict(ev),
                         "official": sum(1 for m in sub if m.get("kind") == "official")})
        f = [x for x in fees if x["product"] == pid]
        rows.append({"product": pid, "source": "bcb_fees", "mentions": len(f),
                     "relevant": len(f), "relevant_pct": 100.0 if f else None,
                     "change_events": sum(1 for x in f if x["in_window"]), "by_event": {},
                     "latest_vigencia": max((x["date"] for x in f), default=None)})
    spikes = []
    for pid in [s["id"] for s in c.subjects()]:
        for ev in ("complaint", "outage", "feature", "launch", "price"):
            cnt = collections.Counter(m.get("published", m.get("date", ""))[:10] for m in ms
                                      if m["product"] == pid and m.get("relevant") and m["event"] == ev)
            series = [cnt.get(d, 0) for d in days()]
            mu = statistics.mean(series)
            sd = statistics.pstdev(series) or 1.0
            for d, n in zip(days(), series):
                z = (n - mu) / sd
                if n >= MIN_SPIKE and z >= Z:
                    spikes.append({"product": pid, "event": ev, "date": d, "count": n,
                                   "mean": round(mu, 2), "z": round(z, 2)})
    c.dump("yield.json", rows)
    c.dump("spikes.json", spikes)
    for r in rows:
        print(r)
    print("spikes:")
    for s in spikes:
        print(s)


if __name__ == "__main__":
    main()
