"""B3 live-quote proxy (`GET /api/quotes`, issue #43) — the dashboard's price ticker.

Source: Yahoo Finance v8 chart endpoint (free, no token; B3 tickers take a `.SA`
suffix). It is fetched SERVER-SIDE — Yahoo does not send browser-friendly CORS, and a
proxy lets us cache to stay well under any rate limit. Each dashboard industry maps to a
few representative listed names; the ticker shows the reps of the selected industry.

Returns ``{"industry": slug, "quotes": [{symbol, price, change, currency}]}``.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

# Representative B3-listed names per industry (base symbols; `.SA` is appended for Yahoo).
# Entry verticals: Fundos (FIAGRO/FII), Crypto (ETFs) have clean tickers; Consórcio and
# Betting are thin on B3, so they fall back to the broad market set. Easily curated.
INDUSTRY_TICKERS: dict[str, list[str]] = {
    "banking": ["ITUB4", "BBDC4", "BBAS3", "SANB11"],
    "insurance": ["BBSE3", "PSSA3", "CXSE3"],
    "investment-banking": ["BPAC11"],
    "asset-management": ["BPAC11"],
    "financial-data-analytics": ["B3SA3"],
    "fintech": ["NUBR33", "INBR32"],
    "agri-funds": ["KNCA11", "RURA11", "VGIA11"],
    "real-estate-funds": ["HGLG11", "KNCR11", "VISC11", "XPML11"],
    "crypto": ["HASH11", "QBTC11"],
}
DEFAULT_TICKERS = ["ITUB4", "BBAS3", "VALE3", "PETR4", "B3SA3"]  # broad market

_YF = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}.SA?interval=1d&range=1d"
_UA = "Mozilla/5.0 (compatible; OncaWarroom/1.0)"
_TTL = 120  # seconds — quotes need only near-live freshness; protects the source
_CACHE: dict[str, tuple[dict[str, Any] | None, float]] = {}  # symbol -> (quote, ts)


def _resp(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _quote(symbol: str) -> dict[str, Any] | None:
    now = time.time()
    hit = _CACHE.get(symbol)
    if hit and now - hit[1] < _TTL:
        return hit[0]
    data: dict[str, Any] | None = None
    try:
        req = urllib.request.Request(_YF.format(sym=symbol), headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=6) as r:
            meta = (json.loads(r.read())["chart"]["result"][0])["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        change = None
        if price is not None and prev:
            change = round((price - prev) / prev * 100, 2)
        if price is not None:
            data = {"symbol": symbol, "price": price, "change": change,
                    "currency": meta.get("currency") or "BRL"}
    except Exception as exc:  # pragma: no cover - best-effort, skip a bad/delisted ticker
        print(f"quote {symbol}: {exc}")
    _CACHE[symbol] = (data, now)
    return data


def _qs(event: dict[str, Any], key: str) -> str:
    return str((event.get("queryStringParameters") or {}).get(key) or "").strip().lower()


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    # Reached same-origin via CloudFront, which injects the shared origin secret; a direct
    # function-URL call without it is rejected (keeps the Yahoo proxy from being open).
    secret = os.environ.get("ONCA_ORIGIN_SECRET")
    if secret:
        headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
        if headers.get("x-onca-origin") != secret:
            return _resp(403, {"error": "forbidden"})
    industry = _qs(event, "industry")
    tickers = INDUSTRY_TICKERS.get(industry) or DEFAULT_TICKERS
    quotes = [q for q in (_quote(t) for t in tickers) if q]
    return _resp(200, {"industry": industry, "quotes": quotes})
