"""Ingest B3 ISE (Índice de Sustentabilidade Empresarial) membership — the free,
open ESG-standing proxy chosen for the "quais bancos têm rating ESG?" coverage
gap (issue #30).

## Why a proxy, not a "rating"

Proprietary ESG agency ratings (MSCI ESG, Sustainalytics, S&P Global ESG,
CDP scores) are the thing the question literally asks for, but they are
paid/access-gated products — confirmed live 2026-08-30:
  - Sustainalytics' public company page 301-redirects (gated flow beyond the
    letter-grade teaser).
  - MSCI's "ESG Ratings & Climate Search Tool" is a lead-gen search page, not
    a data feed (no bulk/programmatic access without a license).
  - CDP has no public bulk-disclosure API (the `/en/responses` search route
    404s; disclosure detail sits behind CDP's own app).
  - BCB's GRSAC report (Resolução CMN 4.945/2021 / Resolução BCB 139/2021) is
    a REAL regulatory disclosure regime, but each institution publishes its
    own PDF on its own site — BCB does not centralize or machine-read them,
    so it is not (yet) a structured, scrapable source.

The best FREE, OPEN, citable substitute is **B3 ISE membership**: B3's own
Índice de Sustentabilidade Empresarial is an equity index whose annual
constituent selection is itself a real, public ESG-standing signal (listed
companies apply, are scored on a public questionnaire administered with
Centro de Estudos em Sustentabilidade/FGV, and only the top-scoring cohort —
~68 of ~500 listed issuers in the 2026-2027 cycle — is admitted). Being an
ISE B3 constituent is a defensible, precisely-citable fact; it is NOT a
numeric ESG score and this module never claims to be one.

## Source

B3's own site reads a JSON endpoint that is **not an officially documented
public API** (found by inspecting network calls, like the WebServices Bovespa
community notes) but requires no auth/key and returned live, current data
when probed 2026-08-30 — same access tier as the rest of Onça's
"public but undocumented" gov/exchange endpoints:

  GET https://sistemaswebb3-listados.b3.com.br/indexProxy/indexCall/GetPortfolioDay/{base64_json}

  where base64_json = {"language":"pt-br","pageNumber":1,"pageSize":<n>,
                        "index":"ISEE","segment":"1"}

`"ISEE"` is B3's internal code for the ISE B3 portfolio (confirmed by probing
candidate codes against the page's own JS artifacts — "ISE" alone returns an
empty result set; "ISEE" returns the real 68-name portfolio). Because this
endpoint is undocumented, treat it as **fragile**: this module fails loudly
(raises) on an unexpected shape rather than returning [] as if "no ESG
signal exists".

## Coverage / cadence

The ISE B3 portfolio rebalances **annually** (new cycle effective ~ May of
the *following* year is announced around Nov/Dec after that year's selection
process; the constituent list itself barely moves day-to-day — this endpoint
returns the live *daily* weights of the current annual portfolio, so treat
the membership list as effectively static within a cycle and re-fetch weekly
at most). Coverage is domestic-B3-listed issuers only: BDR-only fintechs
(Nubank/XP/Stone/PagSeguro/Inter — foreign-primary-listed) can never appear.

## Model

Curated/evidenced registry attribute, same shape as ADR-013
`certifications`/`ownership` — see `src/synth/entity_registry.py`:
`set_esg` / `backfill_esg_ise_b3`, surfaced via `list_entity_attributes()`
(`entity_attrs.esg` in `feed.json`).
"""
from __future__ import annotations

import base64
import datetime as dt
import json
from typing import Any, Callable

INDEX_PAGE_URL = (
    "https://www.b3.com.br/pt_br/market-data-e-indices/indices/"
    "indices-de-sustentabilidade/indice-de-sustentabilidade-empresarial-ise-b3.htm"
)
PORTFOLIO_ENDPOINT = "https://sistemaswebb3-listados.b3.com.br/indexProxy/indexCall/GetPortfolioDay/{token}"
INDEX_CODE = "ISEE"  # B3 internal code for ISE B3 (confirmed live 2026-08-30)


def _token(index: str = INDEX_CODE, page_number: int = 1, page_size: int = 200, segment: str = "1") -> str:
    payload = {
        "language": "pt-br", "pageNumber": page_number, "pageSize": page_size,
        "index": index, "segment": segment,
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _get_json(url: str) -> Any:
    import requests
    resp = requests.get(
        url, timeout=20,
        headers={"User-Agent": "Onca-CI/1.0 (competitive-intelligence)", "Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()


def _to_float(v: Any) -> float | None:
    try:
        return round(float(str(v).replace(",", ".")), 3)
    except (TypeError, ValueError):
        return None


def _parse_date(s: str | None) -> str | None:
    """B3's header date is dd/mm/yy -> ISO yyyy-mm-dd."""
    if not s:
        return None
    try:
        d, m, y = s.split("/")
        yyyy = f"20{y}" if len(y) == 2 else y
        return f"{yyyy}-{int(m):02d}-{int(d):02d}"
    except ValueError:
        return None


def fetch_portfolio(
    *, fetcher: Callable[[str], Any] | None = None, page_size: int = 200,
) -> dict[str, Any]:
    """Fetch the live ISE B3 index portfolio (constituents + as-of date).

    Fails loudly (raises) on transport error or an unrecognized response
    shape — never silently returns an empty portfolio as if the index had no
    members. `fetcher` is injectable for tests.
    """
    fetch = fetcher or _get_json
    url = PORTFOLIO_ENDPOINT.format(token=_token(page_size=page_size))
    data = fetch(url)
    if not isinstance(data, dict) or "results" not in data:
        raise ValueError(f"unexpected ISE B3 portfolio response shape: {type(data)!r}")
    header = data.get("header") or {}
    results = data.get("results") or []
    constituents: list[dict[str, Any]] = []
    for r in results:
        cod = str(r.get("cod") or "").strip()
        if not cod:
            continue
        constituents.append({
            "ticker": cod,
            "asset": str(r.get("asset") or "").strip(),
            "type": str(r.get("type") or "").strip(),
            "weight_pct": _to_float(r.get("part")),
        })
    as_of = _parse_date(header.get("date"))
    return {
        "index": "ISE B3",
        "as_of": as_of,
        # annual cycle label, derived from as_of (ISE B3 cycles run roughly
        # May-to-April); best-effort, not authoritative — real cycle
        # boundaries are announced by B3, not computed.
        "cycle": _cycle_label(as_of),
        "total": (data.get("page") or {}).get("totalRecords") or len(constituents),
        "source_url": url,
        "page_url": INDEX_PAGE_URL,
        "constituents": constituents,
    }


def _cycle_label(as_of: str | None) -> str | None:
    if not as_of:
        return None
    try:
        y = int(as_of[:4])
    except ValueError:
        return None
    return f"{y}-{y + 1}"


def match_tracked_entities(
    portfolio: dict[str, Any], *, ticker_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Map ISE B3 constituents onto Onça's tracked registry entities via the
    curated B3 ticker map (`entity_registry.B3_TICKERS`). Returns normalized,
    cited records — one per matched entity. Unmatched constituents (issuers
    outside the tracked FS universe) are silently skipped; unmatched TRACKED
    entities (not currently an ISE B3 member) simply produce no record —
    absence of a record means "not currently a member", not "unknown"."""
    if ticker_map is None:
        from src.synth.entity_registry import B3_TICKERS
        ticker_map = {ticker: eid for eid, ticker in B3_TICKERS.items()}
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    by_ticker = {c["ticker"]: c for c in portfolio.get("constituents", [])}
    out: list[dict[str, Any]] = []
    for ticker, eid in ticker_map.items():
        hit = by_ticker.get(ticker)
        if not hit:
            continue
        out.append({
            "id": f"b3-ise:{eid}",
            "source": "B3-ISE",
            "kind": "esg_membership",
            "entity": eid,
            "ticker": ticker,
            "asset_name": hit["asset"],
            "weight_pct": hit["weight_pct"],
            "index": portfolio["index"],
            "cycle": portfolio.get("cycle"),
            "as_of": portfolio.get("as_of"),
            "url": portfolio.get("page_url", INDEX_PAGE_URL),
            "fetched_at": fetched_at,
        })
    out.sort(key=lambda r: r["entity"])
    return out


if __name__ == "__main__":  # manual/live smoke test
    pf = fetch_portfolio()
    print(f"ISE B3 as_of={pf['as_of']} cycle={pf['cycle']} total={pf['total']} "
          f"source={pf['source_url']}")
    recs = match_tracked_entities(pf)
    print(f"matched {len(recs)} tracked entities:")
    for r in recs:
        print(f"  {r['entity']:<16} {r['ticker']:<8} weight={r['weight_pct']}% "
              f"as_of={r['as_of']} url={r['url']}")
