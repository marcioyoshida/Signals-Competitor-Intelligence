"""Apple App Store customer-reviews RSS pull (#158 spike).

Public, keyless Apple feed: https://itunes.apple.com/br/rss/customerreviews/page=N/id=<appId>/sortby=mostrecent/json
Apple caps it at 10 pages x 50 reviews (the 500 most recent). For a high-volume app 500 reviews
may cover < 30 days, so coverage (oldest review date reached) is recorded per app — a truncated
window is a finding, not a silent gap. Also records the iTunes lookup metadata (current version,
its release date and release notes = the app's own "What's New"). Output: out/appstore.json,
out/appstore_meta.json.
"""
from __future__ import annotations

import common as c

RSS = "https://itunes.apple.com/br/rss/customerreviews/page={page}/id={app}/sortby=mostrecent/json"


def pull_app(app_id: str) -> tuple[list[dict], dict]:
    rows, pages = [], 0
    for page in range(1, 11):
        try:
            d = c.get_json(RSS.format(page=page, app=app_id), pace=1.0)
        except RuntimeError as e:
            print(f"  page {page}: {e}")
            break
        entries = d.get("feed", {}).get("entry", [])
        if isinstance(entries, dict):
            entries = [entries]
        entries = [e for e in entries if "im:rating" in e]   # page 1 may lead with the app entry
        if not entries:
            break
        pages += 1
        for e in entries:
            rows.append({"review_id": e["id"]["label"], "date": e["updated"]["label"],
                         "rating": int(e["im:rating"]["label"]), "version": e.get("im:version", {}).get("label"),
                         "title": e["title"]["label"], "text": e["content"]["label"],
                         "author": None})   # author name deliberately not stored (ADR-0003 #3, minimize PII)
        if min(r["date"] for r in rows)[:10] < c.WINDOW_START:
            break
    dates = sorted(r["date"][:10] for r in rows)
    cov = {"pages": pages, "reviews": len(rows), "oldest": dates[0] if dates else None,
           "newest": dates[-1] if dates else None,
           "covers_window": bool(dates) and dates[0] <= c.WINDOW_START}
    return rows, cov


def main() -> None:
    out, meta = [], {}
    for s in c.subjects():
        for app in s["apple_app_ids"]:
            look = c.get_json("https://itunes.apple.com/lookup", {"id": app["id"], "country": "br"})["results"][0]
            rows, cov = pull_app(app["id"])
            inwin = [r for r in rows if c.in_window(r["date"])]
            meta[s["id"]] = {"app_id": app["id"], "coverage": cov, "in_window": len(inwin),
                             "version": look.get("version"),
                             "version_date": look.get("currentVersionReleaseDate"),
                             "release_notes": look.get("releaseNotes"),
                             "avg_rating_all_time": look.get("averageUserRating"),
                             "rating_count": look.get("userRatingCount"), "fetched_at": c.dt_now()}
            for r in inwin:
                out.append({"product": s["id"], "source": "appstore", "app_id": app["id"],
                            "url": f"https://apps.apple.com/br/app/id{app['id']}?see-all=reviews",
                            "fetched_at": c.dt_now(), **r})
            print(s["id"], cov, "in_window", len(inwin))
    c.dump("appstore.json", out)
    c.dump("appstore_meta.json", meta)


if __name__ == "__main__":
    main()
