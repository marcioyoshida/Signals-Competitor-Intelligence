"""#93 — market valuation (market cap, P/B, P/L) for listed tracked issuers.

Inputs: shares outstanding per class from CVM ``composicao_capital`` (carried on the
financials store by ``cvm_financials``) × the live per-class price (Yahoo v8 chart, the
same endpoint ``quotes_api`` uses — v7's ``marketCap`` is 401 and was the original
blocker). Computed at feed-build time rather than in the monthly financials run, so the
price is at most a day old instead of a month.

Two traps, both measured live on 2026-09-23 rather than assumed:

1. **CVM share counts carry no unit.** Banco do Brasil files 5,730,834,040 shares; Itaú
   files 11,026,869 and Santander 7,487,527 — both in THOUSANDS, with nothing in the file
   saying so. The scale is resolved against book value: the unscaled price-to-book of a
   thousands-filer lands near 0.002, which no listed bank trades at. A read that fits the
   plausible band neither raw nor ×1000 is WITHHELD, never forced.

2. **One ticker is not the company.** ``SANB11`` is a unit (1 ON + 1 PN), ``ITUB4`` prices
   only the preferred class. Market cap is therefore Σ(class price × class shares), with the
   ON price from ``<root>3`` and the PN price from the first of ``<root>4/5/6`` that quotes.
   Every class that has shares must have a price, or the entity is withheld. BDRs
   (``…32``–``…35``) are skipped: their conversion ratio is not in this data.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.request
from typing import Any, Callable

_YF = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}.SA?interval=1d&range=1d"
_UA = "Mozilla/5.0 (compatible; OncaFeed/1.0)"
_RE_TICKER = re.compile(r"^([A-Z0-9]{4})(\d{1,2})$")  # roots can hold a digit: B3SA3
_BDR_SUFFIXES = {"32", "33", "34", "35"}
# Plausible price-to-book band for a listed issuer. Deliberately wide — it only has to
# separate "right unit" from "off by 1000×", and those sit three orders of magnitude apart.
_PB_BAND = (0.05, 50.0)
_IMMATERIAL = 0.001  # a share class below 0.1% of the total is ignored


def fetch_price(symbol: str, *, timeout: int = 6) -> dict[str, Any] | None:
    """Last traded price for a B3 symbol; None on any error (best-effort)."""
    try:
        req = urllib.request.Request(_YF.format(sym=symbol), headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            meta = json.loads(r.read())["chart"]["result"][0]["meta"]
    except Exception as exc:  # pragma: no cover - network
        print(f"valuation: quote {symbol}: {exc}")
        return None
    price, ts = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if not price:
        return None
    day = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat() if ts else None
    return {"price": float(price), "date": day}


def _class_prices(ticker: str, quote: Callable[[str], dict[str, Any] | None],
                  need_on: bool, need_pn: bool) -> dict[str, dict[str, Any]] | None:
    m = _RE_TICKER.match(str(ticker or "").upper().strip())
    if not m or m.group(2) in _BDR_SUFFIXES:
        return None
    root, out = m.group(1), {}
    if need_on:
        q = quote(root + "3")
        if not q:
            return None
        out["on"] = q
    if need_pn:
        q = next((q for q in (quote(root + s) for s in ("4", "5", "6")) if q), None)
        if not q:
            return None
        out["pn"] = q
    return out


def value(rec: dict[str, Any], ticker: str,
          quote: Callable[[str], dict[str, Any] | None] = fetch_price) -> dict[str, Any] | None:
    """Valuation block for one financials record, or None when it cannot be stated honestly."""
    src = rec.get("interim") if (rec.get("interim") or {}).get("shares_as_of") else rec
    on, pn = float(src.get("shares_on") or 0), float(src.get("shares_pn") or 0)
    equity = src.get("equity") or rec.get("equity")
    if on + pn <= 0 or not equity or equity <= 0:
        return None
    # An immaterial class (IRB files ONE preferred share — a golden share that never
    # trades) must not withhold the whole company for want of a quote it cannot have.
    on, pn = (0.0 if on / (on + pn) < _IMMATERIAL else on), (0.0 if pn / (on + pn) < _IMMATERIAL else pn)
    prices = _class_prices(ticker, quote, on > 0, pn > 0)
    if not prices:
        return None
    raw = on * (prices.get("on") or {}).get("price", 0) + pn * (prices.get("pn") or {}).get("price", 0)
    scale = next((s for s in (1, 1000) if _PB_BAND[0] <= raw * s / equity <= _PB_BAND[1]), None)
    if scale is None:
        return None
    mcap = raw * scale
    # P/L only on a 12-month profit: an ITR's 6-month net income would double the multiple.
    ni = rec.get("net_income") if rec.get("months") == 12 else None
    return {
        "market_cap": round(mcap, 2),
        "pb": round(mcap / equity, 2),
        "pe": round(mcap / ni, 1) if ni and ni > 0 else None,
        "price_date": max((p.get("date") or "") for p in prices.values()) or None,
        "shares_as_of": src.get("shares_as_of"),
        "share_scale": scale,
    }


def attach(records: list[dict[str, Any]], tickers: dict[str, str],
           quote: Callable[[str], dict[str, Any] | None] = fetch_price) -> list[dict[str, Any]]:
    """Add ``valuation`` to each financials record whose entity has a ticker. In place."""
    cache: dict[str, dict[str, Any] | None] = {}

    def _q(sym: str) -> dict[str, Any] | None:
        if sym not in cache:
            cache[sym] = quote(sym)
        return cache[sym]

    for rec in records:
        t = tickers.get(rec.get("entity_id"))
        if t:
            v = value(rec, t, _q)
            if v:
                rec["valuation"] = v
    return records
