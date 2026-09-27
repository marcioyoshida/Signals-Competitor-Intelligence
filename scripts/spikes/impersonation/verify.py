"""Hand-verification fetch (#160 rule V): re-fetch each proposal's channel + in-window videos via
the API so the harm signal can be QUOTED from a fresh pull. Writes out/verify.json. Prints for a
human; decides nothing.
Usage: python verify.py <channelId> [<channelId> ...]
"""
from __future__ import annotations

import sys

import common as c


def main(ids: list[str]) -> None:
    out = {}
    meta = c.channels(ids, "verify channels")
    for cid in ids:
        m = meta.get(cid)
        if not m:
            print(cid, "NOT FOUND"); continue
        up = m["contentDetails"]["relatedPlaylists"]["uploads"]
        d = c.yt("playlistItems", "verify uploads", part="contentDetails", playlistId=up, maxResults=10)
        vids = [i["contentDetails"]["videoId"] for i in d.get("items", [])]
        vmeta = c.yt("videos", "verify videos", part="snippet,statistics,status", id=",".join(vids)).get("items", []) if vids else []
        out[cid] = {"channel": m, "videos": vmeta, "fetched_at": c.dt_now()}
        sn = m["snippet"]
        print(f"\n### {cid} {sn['title']!r} {sn.get('customUrl')} created {sn['publishedAt'][:10]} "
              f"country={sn.get('country')} stats={m.get('statistics')}")
        print("  channel desc:", repr(sn.get("description", "")[:600]))
        for v in vmeta:
            vs = v["snippet"]
            print(f"  - {v['id']} {vs['publishedAt'][:10]} views={v['statistics'].get('viewCount')} {vs['title']!r}")
            print("      desc:", repr(vs.get("description", "")[:700]))
    prev = c.load("verify.json") if (c.OUT / "verify.json").exists() else {}
    prev.update(out)
    c.dump("verify.json", prev)
    print("\nunits:", c.quota()["units"])


if __name__ == "__main__":
    main(sys.argv[1:])
