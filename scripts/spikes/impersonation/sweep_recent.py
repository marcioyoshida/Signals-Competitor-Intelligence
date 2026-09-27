"""Second sweep (#160): newest in-window videos per brand (order=date), to catch NEW look-alike
channels that relevance-ranked search buries; plus the PicPay Google Play title for the triage
(the Play listing is "Banco PicPay: Cartão, Pix e +", unlike the App Store title).
Cost: 6 searches (600) + hydration. Same rules.py; output out/sweep_recent.json.
"""
from __future__ import annotations

import common as c
from rules import harm_signals, is_lookalike, norm
from scan_harm import APP_TITLES, QUERIES, WIN


def main() -> None:
    known = {r["id"] for rows in c.load("scan.json").values() for r in rows}
    out = {}
    for s in c.subjects():
        sid = s["id"]
        official = {ch["id"] for ch in s["youtube_channels"]}
        d = c.yt("search", f"{sid} recent", part="snippet", type="video", q=QUERIES[sid], order="date",
                 maxResults=50, regionCode="BR", relevanceLanguage="pt", **WIN)
        items = d.get("items", [])
        by_ch: dict[str, list] = {}
        for it in items:
            by_ch.setdefault(it["snippet"]["channelId"], []).append(it["id"]["videoId"])
        meta = c.channels(list(by_ch), f"{sid} recent hydrate")
        rows = []
        for cid, vids in by_ch.items():
            m = meta.get(cid)
            if not m or cid in official:
                continue
            sn = m["snippet"]
            la, sim = is_lookalike(s, sn["title"], sn.get("customUrl") or "", APP_TITLES[sid])
            if not la:
                continue
            v = c.yt("videos", f"{sid} recent videos", part="snippet", id=",".join(vids[:50])).get("items", [])
            rows.append({"id": cid, "title": sn["title"], "handle": sn.get("customUrl"), "created": sn["publishedAt"],
                         "stats": m.get("statistics"), "new_vs_scan": cid not in known,
                         "desc_harm": harm_signals(sid, sn.get("description", "")),
                         "videos": [{"id": x["id"], "title": x["snippet"]["title"], "published": x["snippet"]["publishedAt"],
                                     "harm": harm_signals(sid, x["snippet"]["title"] + "\n" + x["snippet"].get("description", ""))}
                                    for x in v]})
        out[sid] = {"results": len(items), "channels": len(by_ch), "lookalikes": rows}
        print(f"{sid:13} results={len(items)} channels={len(by_ch)} lookalikes={len(rows)} "
              f"new={sum(r['new_vs_scan'] for r in rows)} units={c.quota()['units']}")
    # PicPay Play-store title, for the triage
    d = c.yt("search", "triage picpay play title", part="snippet", type="channel", q="Banco PicPay: Cartão, Pix e +",
             maxResults=50, regionCode="BR")
    ids = [it["id"]["channelId"] for it in d.get("items", [])]
    meta = c.channels(ids, "triage picpay play hydrate")
    tgt = norm("Banco PicPay: Cartão, Pix e +")
    ex = [{"id": k, "title": m["snippet"]["title"], "created": m["snippet"]["publishedAt"], "stats": m.get("statistics"),
           "desc": m["snippet"].get("description", ""), "handle": m["snippet"].get("customUrl")}
          for k, m in meta.items() if norm(m["snippet"]["title"]) == tgt]
    out["_picpay_play_title"] = {"results": len(ids), "exact": ex}
    print("picpay play-title exact channels:", len(ex), "units", c.quota()["units"])
    c.dump("sweep_recent.json", out)


if __name__ == "__main__":
    main()
