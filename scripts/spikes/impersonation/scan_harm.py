"""Look-alike + harm-signal scan per issuer (#160 task 2). Applies rules.py (fixed pre-data).

Per issuer (4 searches = 400 units): channel search on the brand, channel search on
"<brand> suporte", and two in-window video searches on offer terms (YouTube `|` = OR). Every
channel surfaced is hydrated; look-alikes (rule L) get their uploads playlist read (1 unit) and
their in-window videos hydrated (1 unit/50) so H1-H4 and W can be evaluated. Output is PROPOSALS
for hand verification (verify.py), never a verdict.
"""
from __future__ import annotations

import common as c
from rules import harm_signals, is_lookalike, norm

QUERIES = {
    "nubank": "Nubank", "inter": "Banco Inter", "picpay": "PicPay",
    "mercado_pago": "Mercado Pago", "c6": "C6 Bank",
}
APP_TITLES = {"nubank": "Nubank: Conta, Cartão e mais", "inter": "Inter: Conta, Cartão e Pix",
              "picpay": "PicPay: Conta, Cartão e Pix", "mercado_pago": "Mercado Pago: banco digital",
              "c6": "C6 Bank: Cartão, conta e mais!"}
WIN = {"publishedAfter": f"{c.WINDOW_START}T00:00:00Z", "publishedBefore": "2026-09-27T00:00:00Z"}
UPLOAD_CAP = 60   # playlistItems reads per issuer


def search(note: str, **p) -> list[dict]:
    d = c.yt("search", note, part="snippet", maxResults=50, regionCode="BR", relevanceLanguage="pt", **p)
    return d.get("items", [])


def main() -> None:
    triage = c.load("triage.json")
    dormant_ids = {r["id"] for v in triage.values() for r in v["channels"] if r["exact_title"]}
    out = {}
    for s in c.subjects():
        sid, q = s["id"], QUERIES[s["id"]]
        official = {ch["id"] for ch in s["youtube_channels"]}
        hits: dict[str, dict] = {}   # channel id -> how it was found
        for it in search(f"{sid} ch brand", type="channel", q=q):
            hits.setdefault(it["id"]["channelId"], {"via": []})["via"].append("ch:brand")
        for it in search(f"{sid} ch suporte", type="channel", q=f"{q} suporte"):
            hits.setdefault(it["id"]["channelId"], {"via": []})["via"].append("ch:suporte")
        vids: dict[str, str] = {}
        for tag, qq in (("v:support", f"{q} suporte|atendimento|whatsapp|central"),
                        ("v:offer", f"{q} empréstimo|desbloqueio|liberar|pix na hora")):
            for it in search(f"{sid} {tag}", type="video", q=qq, **WIN):
                cid = it["snippet"]["channelId"]
                hits.setdefault(cid, {"via": []})["via"].append(tag)
                vids[it["id"]["videoId"]] = cid
        meta = c.channels(list(hits), f"{sid} hydrate")
        rows, reads = [], 0
        for cid, h in hits.items():
            m = meta.get(cid)
            if not m:
                continue
            sn, st = m["snippet"], m.get("statistics", {})
            la, sim = is_lookalike(s, sn.get("title", ""), sn.get("customUrl") or "", APP_TITLES[sid])
            r = {"issuer": sid, "id": cid, "title": sn.get("title"), "handle": sn.get("customUrl"),
                 "published": sn.get("publishedAt"), "country": sn.get("country"),
                 "description": sn.get("description", ""), "subs": int(st.get("subscriberCount", 0) or 0),
                 "videos": int(st.get("videoCount", 0) or 0), "views": int(st.get("viewCount", 0) or 0),
                 "via": sorted(set(h["via"])), "official": cid in official, "lookalike": la and cid not in official,
                 "sim": sim, "exact_app_title_dormant": cid in dormant_ids}
            if r["lookalike"]:
                r["harm_channel_desc"] = harm_signals(sid, r["description"])
                if r["videos"] and reads < UPLOAD_CAP:
                    reads += 1
                    uploads = m["contentDetails"]["relatedPlaylists"]["uploads"]
                    try:
                        d = c.yt("playlistItems", f"{sid} uploads", part="contentDetails",
                                 playlistId=uploads, maxResults=50)
                        items = d.get("items", [])
                        pubs = [i["contentDetails"].get("videoPublishedAt", "") for i in items]
                        r["uploads_in_window"] = [i["contentDetails"]["videoId"] for i, p in zip(items, pubs) if c.in_window(p)]
                        r["uploads_before_window"] = sum(1 for p in pubs if p and p[:10] < c.WINDOW_START)
                        r["uploads_page_full"] = bool(d.get("nextPageToken"))
                    except RuntimeError as e:
                        r["uploads_error"] = str(e)[:160]
                # in-window videos: from uploads + from the video searches
                win_ids = list(dict.fromkeys(r.get("uploads_in_window", []) + [v for v, ch in vids.items() if ch == cid]))[:50]
                r["window_videos"] = []
                if win_ids:
                    d = c.yt("videos", f"{sid} win videos", part="snippet,statistics", id=",".join(win_ids))
                    for v in d.get("items", []):
                        vs = v["snippet"]
                        if not c.in_window(vs["publishedAt"]):
                            continue
                        r["window_videos"].append({
                            "id": v["id"], "title": vs["title"], "published": vs["publishedAt"],
                            "views": int(v.get("statistics", {}).get("viewCount", 0) or 0),
                            "description": vs.get("description", "")[:3000],
                            "harm": harm_signals(sid, vs["title"] + "\n" + vs.get("description", ""))})
                n_win = len(r.get("uploads_in_window", []))
                r["H4_surge"] = n_win >= 3 and (r.get("uploads_before_window", 1) == 0 and not r.get("uploads_page_full")
                                                 or c.in_window(r["published"]))
                r["W_created_in_window"] = c.in_window(r["published"])
                r["harm_any"] = bool(r["harm_channel_desc"] or r["H4_surge"] or any(v["harm"] for v in r["window_videos"]))
                r["proposal"] = r["harm_any"] and (bool(r["window_videos"] and (any(v["harm"] for v in r["window_videos"]) or r["harm_channel_desc"]))
                                                   or r["H4_surge"] or (r["W_created_in_window"] and r["harm_channel_desc"]))
            rows.append(r)
        out[sid] = rows
        la = [r for r in rows if r["lookalike"]]
        print(f"{sid:13} channels={len(rows):3} lookalikes={len(la):3} dormant={sum(1 for r in la if r['videos']==0 and r['subs']==0):3} "
              f"harm_any={sum(1 for r in la if r.get('harm_any')):3} proposals={sum(1 for r in la if r.get('proposal')):3} "
              f"units={c.quota()['units']}")
    c.dump("scan.json", out)


if __name__ == "__main__":
    main()
