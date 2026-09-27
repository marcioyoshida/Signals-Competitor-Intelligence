"""App Store look-alike apps (#160 task 3): iTunes Search API, country=br, keyless, free.

Per issuer: search the brand (limit 200) and flag apps that are NOT published by the official
seller but whose name or seller name carries a brand alias (rules.is_lookalike). Harm signal for an
app = rules.harm_signals on its description + a non-official seller. In window = releaseDate or
currentVersionReleaseDate in the 30-day window. Output out/appstore_lookalikes.json.
"""
from __future__ import annotations

import common as c
from rules import harm_signals, is_lookalike, norm

OFFICIAL_SELLERS = {  # from iTunes lookup of the official app IDs (subjects.json), 2026-09-26
    "nubank": {"nu pagamentos", "nu financeira", "nu invest", "nubank"},
    "inter": {"banco intermedium", "banco inter", "inter&co", "inter distribuidora"},
    "picpay": {"picpay"},
    "mercado_pago": {"mercadolibre", "mercado pago", "mercadolivre"},
    "c6": {"banco c6", "c6 bank"},
}
TERMS = {"nubank": ["Nubank"], "inter": ["Banco Inter", "Inter conta"], "picpay": ["PicPay"],
         "mercado_pago": ["Mercado Pago"], "c6": ["C6 Bank"]}


def main() -> None:
    out = {}
    for s in c.subjects():
        sid = s["id"]
        seen, rows = set(), []
        for t in TERMS[sid]:
            d = c.get_json("https://itunes.apple.com/search",
                           {"term": t, "country": "br", "entity": "software", "limit": 200}, pace=1.0)
            for a in d.get("results", []):
                if a["trackId"] in seen:
                    continue
                seen.add(a["trackId"])
                seller = (a.get("sellerName") or a.get("artistName") or "")
                official = any(norm(o) in norm(seller) for o in OFFICIAL_SELLERS[sid])
                la_name, sim = is_lookalike(s, a["trackName"], "", "")
                la_seller, _ = is_lookalike(s, seller, "", "")
                if official or not (la_name or la_seller):
                    continue
                rows.append({"issuer": sid, "trackId": a["trackId"], "name": a["trackName"], "seller": seller,
                             "genre": a.get("primaryGenreName"), "released": a.get("releaseDate"),
                             "updated": a.get("currentVersionReleaseDate"), "ratings": a.get("userRatingCount"),
                             "url": a.get("trackViewUrl", "").split("?")[0],
                             "in_window": c.in_window(a.get("releaseDate", "")) or c.in_window(a.get("currentVersionReleaseDate", "")),
                             "harm": harm_signals(sid, a.get("description", ""))[:6],
                             "description": a.get("description", "")[:1500]})
        out[sid] = {"searched": len(seen), "lookalikes": rows}
        print(f"{sid:13} apps={len(seen):3} lookalike_apps={len(rows)} in_window={sum(r['in_window'] for r in rows)}")
        for r in rows:
            print(f"    {r['trackId']} {r['name']!r} by {r['seller']!r} [{r['genre']}] rel={r['released'][:10]} "
                  f"upd={(r['updated'] or '')[:10]} ratings={r['ratings']} harm={[h['match'] for h in r['harm']]}")
    c.dump("appstore_lookalikes.json", out)


if __name__ == "__main__":
    main()
