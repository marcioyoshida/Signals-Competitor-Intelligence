"""Triage the C6 look-alike channel cluster (#160 task 1).

Hypothesis under test (benign): Google Ads App campaigns auto-create a YouTube channel named after
the app. Predictions if TRUE: (a) the pattern recurs for OTHER apps that buy app installs, incl. an
unrelated control set; (b) channel title == the store title EXACTLY (machine-made), not a variation;
(c) no description/branding/handle, no public uploads; (d) creation in batches.
Predictions if it is a human impersonation farm: variations/typos, descriptions or links, handles,
some public content, and concentration on banks rather than on any app that advertises.

Cost: 1-2 search pages per title (100 each) + channels.list (1/50) + playlistItems (1/channel).
"""
from __future__ import annotations

import collections

import common as c
from rules import norm

TITLES = {  # exact App Store (BR) titles from iTunes lookup/search, 2026-09-26
    "c6": ("C6 Bank: Cartão, conta e mais!", 1463463143),
    "nubank": ("Nubank: Conta, Cartão e mais", 814456780),
    "inter": ("Inter: Conta, Cartão e Pix", 839711154),
    "picpay": ("PicPay: Conta, Cartão e Pix", 561524792),
    "mercado_pago": ("Mercado Pago: banco digital", 925436649),
    # controls: large unrelated BR apps that also buy installs
    "ctl_ifood": ("iFood: pedir delivery em casa", 483017239),
    "ctl_shopee": ("Shopee: Compre de Tudo Online", 1481812175),
    "ctl_kwai": ("Kwai - Vídeo & Bônus Diário", 1338605092),
}
PAGES = {"c6": 2}
OFFICIAL = {s["id"]: {ch["id"] for ch in s["youtube_channels"]} for s in c.subjects()}


def search_channels(key: str, title: str) -> list[str]:
    ids, token = [], None
    for _ in range(PAGES.get(key, 1)):
        p = {"part": "snippet", "type": "channel", "q": title, "maxResults": 50, "regionCode": "BR"}
        if token:
            p["pageToken"] = token
        d = c.yt("search", f"triage channel search {key}", **p)
        ids += [it["id"]["channelId"] for it in d.get("items", []) if it.get("id", {}).get("channelId")]
        token = d.get("nextPageToken")
        if not token:
            break
    return ids


def main() -> None:
    res = {}
    for key, (title, app_id) in TITLES.items():
        ids = search_channels(key, title)
        meta = c.channels(ids, f"triage hydrate {key}")
        target = norm(title)
        rows = []
        for cid in ids:
            m = meta.get(cid)
            if not m:
                continue
            sn, st, br = m["snippet"], m.get("statistics", {}), m.get("brandingSettings", {})
            t = sn.get("title", "")
            rows.append({
                "id": cid, "title": t, "exact_title": norm(t) == target,
                "contains_title": target in norm(t) or norm(t) in target and len(norm(t)) > 8,
                "handle": sn.get("customUrl"), "published": sn.get("publishedAt"),
                "country": sn.get("country"), "default_language": sn.get("defaultLanguage"),
                "description": sn.get("description", ""),
                "branding_channel": br.get("channel", {}), "has_banner": bool(br.get("image")),
                "subs": int(st.get("subscriberCount", 0) or 0), "hidden_subs": st.get("hiddenSubscriberCount"),
                "videos": int(st.get("videoCount", 0) or 0), "views": int(st.get("viewCount", 0) or 0),
                "uploads": m.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"),
                "status": m.get("status", {}), "topics": m.get("topicDetails", {}),
                "official": cid in OFFICIAL.get(key, set()),
            })
        # uploads playlist check for exact/contains-title look-alikes (does it list anything?)
        for r in rows:
            if (r["exact_title"] or r["contains_title"]) and not r["official"] and r["uploads"]:
                try:
                    d = c.yt("playlistItems", f"triage uploads {key}", part="snippet,status",
                             playlistId=r["uploads"], maxResults=5)
                    r["uploads_items"] = [{"title": i["snippet"]["title"], "privacy": i.get("status", {}).get("privacyStatus"),
                                           "published": i["snippet"]["publishedAt"]} for i in d.get("items", [])]
                    r["uploads_total"] = d.get("pageInfo", {}).get("totalResults")
                except RuntimeError as e:
                    r["uploads_error"] = str(e)[:160]
        res[key] = {"title": title, "app_id": app_id, "channels": rows}
        ex = [r for r in rows if r["exact_title"] and not r["official"]]
        print(f"{key:14} results={len(rows):3} exact_title={len(ex):3} "
              f"zero_everything={sum(1 for r in ex if r['subs']==0 and r['videos']==0 and r['views']==0):3} "
              f"dates={collections.Counter((r['published'] or '')[:10] for r in ex).most_common(4)}")
    c.dump("triage.json", res)
    print("units so far:", c.quota()["units"])


if __name__ == "__main__":
    main()
