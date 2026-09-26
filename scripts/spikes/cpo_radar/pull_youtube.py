"""YouTube Data API v3 pull (#158 spike): official-channel uploads + creator mentions in the window.

Quota (units): playlistItems.list = 1/page, search.list = 100/page, videos.list = 1/call.
Budget per run is logged to out/youtube_quota.json. The key is Bluefin's (shared quota, ADR 0004
there) — keep pages small. Output: out/youtube.json (one row per video, with full description).
"""
from __future__ import annotations

import common as c

API = "https://www.googleapis.com/youtube/v3"
SEARCH_PAGES = 2          # 2 x 50 results per product = 200 units per product
quota = {"playlistItems": 0, "search": 0, "videos": 0}


def official_uploads(ch: dict, key: str) -> list[dict]:
    uploads = "UU" + ch["id"][2:]
    rows, token = [], None
    while True:
        p = {"part": "snippet,contentDetails", "playlistId": uploads, "maxResults": 50, "key": key}
        if token:
            p["pageToken"] = token
        d = c.get_json(f"{API}/playlistItems", p)
        quota["playlistItems"] += 1
        stop = False
        for it in d.get("items", []):
            pub = it["contentDetails"].get("videoPublishedAt") or it["snippet"]["publishedAt"]
            if pub[:10] < c.WINDOW_START:
                stop = True
                continue
            if c.in_window(pub):
                rows.append({"video_id": it["contentDetails"]["videoId"], "published": pub})
        token = d.get("nextPageToken")
        if stop or not token:
            return rows


def creator_search(query: str, key: str) -> list[dict]:
    rows, token = [], None
    for _ in range(SEARCH_PAGES):
        p = {"part": "snippet", "type": "video", "q": query, "regionCode": "BR",
             "relevanceLanguage": "pt", "maxResults": 50, "order": "relevance",
             "publishedAfter": f"{c.WINDOW_START}T00:00:00Z",
             "publishedBefore": "2026-09-27T00:00:00Z", "key": key}
        if token:
            p["pageToken"] = token
        d = c.get_json(f"{API}/search", p)
        quota["search"] += 100
        rows += [{"video_id": it["id"]["videoId"], "published": it["snippet"]["publishedAt"]}
                 for it in d.get("items", []) if it.get("id", {}).get("videoId")]
        token = d.get("nextPageToken")
        if not token:
            break
    return rows


def hydrate(ids: list[str], key: str) -> dict[str, dict]:
    out = {}
    for i in range(0, len(ids), 50):
        d = c.get_json(f"{API}/videos", {"part": "snippet,statistics,contentDetails",
                                          "id": ",".join(ids[i:i + 50]), "key": key})
        quota["videos"] += 1
        for it in d.get("items", []):
            s = it["snippet"]
            out[it["id"]] = {"channel_id": s["channelId"], "channel": s["channelTitle"],
                             "title": s["title"], "description": s.get("description", ""),
                             "published": s["publishedAt"], "lang": s.get("defaultAudioLanguage"),
                             "views": int(it.get("statistics", {}).get("viewCount", 0) or 0),
                             "duration": it.get("contentDetails", {}).get("duration")}
    return out


def main() -> None:
    key = c.youtube_key()
    rows = []
    for s in c.subjects():
        official_ids = {ch["id"] for ch in s["youtube_channels"]}
        off = [dict(r, kind="official") for ch in s["youtube_channels"] for r in official_uploads(ch, key)]
        q = f'"{s["aliases"][0]}"' if " " in s["aliases"][0] else s["aliases"][0]
        cre = [dict(r, kind="creator") for r in creator_search(q, key)]
        meta = hydrate(list({r["video_id"] for r in off + cre}), key)
        seen = set()
        for r in off + cre:
            m = meta.get(r["video_id"])
            if not m or r["video_id"] in seen:
                continue
            seen.add(r["video_id"])
            kind = "official" if m["channel_id"] in official_ids else "creator"
            rows.append({"product": s["id"], "source": "youtube", "kind": kind, "query": q,
                         "url": f"https://www.youtube.com/watch?v={r['video_id']}",
                         "fetched_at": c.dt_now(), **m, "video_id": r["video_id"]})
        print(f"{s['id']}: official={sum(1 for x in rows if x['product']==s['id'] and x['kind']=='official')}"
              f" creator={sum(1 for x in rows if x['product']==s['id'] and x['kind']=='creator')}")
    c.dump("youtube.json", rows)
    c.dump("youtube_quota.json", dict(quota, total=sum(quota.values())))
    print("quota units:", quota, sum(quota.values()))


if __name__ == "__main__":
    main()
