"""Ingest SEC EDGAR filings — US-listed Brazilian fintech signals.

Several Brazilian fintechs are US-listed and file their richest financials
(revenue, TPV, take rate, active clients) with the SEC, not CVM:
  Stone (STNE), PagSeguro/PagBank (PAGS), Nu Holdings (NU),
  Inter&Co (INTR), XP Inc (XP).
Most are foreign private issuers → annual 20-F + interim/material 6-K
(rather than 10-K/10-Q/8-K). A NEW filing appearing is the signal.

Two free EDGAR endpoints (no key; SEC REQUIRES a descriptive User-Agent
with contact info, and asks for <=10 req/s):
  1. Ticker→CIK map: https://www.sec.gov/files/company_tickers.json
  2. Per-company recent filings: https://data.sec.gov/submissions/CIK##########.json

User-Agent (priority):
  1. ONCA_SEC_USER_AGENT env
  2. Module DEFAULT_USER_AGENT (set a real contact before production)

Lambda port: detect_new via DynamoDB; first run seeds baseline.
"""
from __future__ import annotations

import datetime as dt
import os
import time
from typing import Any

import requests

# SEC fair-access: identify product + contact email.
# Override in Lambda via ONCA_SEC_USER_AGENT.
# SEC rejects some UA shapes with 403; keep it short: product name + email.
DEFAULT_USER_AGENT = "Onca Competitive Intelligence marcioyoshida@gmail.com"

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"

# Forms worth surfacing. FPIs use 20-F / 6-K; domestic filers 10-K/10-Q/8-K.
DEFAULT_FORMS = {"20-F", "6-K", "10-K", "10-Q", "8-K", "F-1", "424B4", "6-K/A", "20-F/A"}

# Pause between per-CIK requests (SEC asks for polite rate limiting).
REQUEST_PAUSE_SEC = 0.15


def _headers() -> dict[str, str]:
    ua = (os.environ.get("ONCA_SEC_USER_AGENT") or DEFAULT_USER_AGENT).strip()
    return {"User-Agent": ua}


def resolve_ciks(tickers: list[str]) -> dict[str, str]:
    """Map tickers (e.g. STNE) to zero-padded 10-digit CIKs via EDGAR."""
    resp = requests.get(TICKERS_URL, headers=_headers(), timeout=60)
    resp.raise_for_status()
    want = {t.upper() for t in tickers}
    out: dict[str, str] = {}
    for row in resp.json().values():
        tk = str(row.get("ticker", "")).upper()
        if tk in want:
            out[tk] = str(row["cik_str"]).zfill(10)
    return out


def fetch_filings(
    tickers: list[str],
    forms: set[str] | None = None,
    lookback_days: int | None = 365,
    max_per_ticker: int = 40,
) -> list[dict[str, Any]]:
    """Fetch recent filings for the given tickers, filtered to `forms`.

    lookback_days: keep filings with filingDate on/after today - N days.
      None = no date filter (not recommended for first seed — hundreds of rows).
    max_per_ticker: cap after form+date filter (most-recent first).
    """
    if not tickers:
        return []
    forms = forms or DEFAULT_FORMS
    ciks = resolve_ciks(tickers)
    cutoff: dt.date | None = None
    if lookback_days is not None:
        cutoff = dt.date.today() - dt.timedelta(days=lookback_days)

    out: list[dict[str, Any]] = []
    for i, (ticker, cik10) in enumerate(ciks.items()):
        if i:
            time.sleep(REQUEST_PAUSE_SEC)
        resp = requests.get(
            SUBMISSIONS_URL.format(cik10=cik10), headers=_headers(), timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        name = data.get("name")
        recent = data.get("filings", {}).get("recent", {})
        forms_col = recent.get("form", [])
        dates_col = recent.get("filingDate", [])
        acc_col = recent.get("accessionNumber", [])
        doc_col = recent.get("primaryDocument", [])
        per_ticker: list[dict[str, Any]] = []
        for idx, form in enumerate(forms_col):
            if form not in forms:
                continue
            filed = dates_col[idx] if idx < len(dates_col) else None
            if cutoff and filed:
                try:
                    filed_d = dt.date.fromisoformat(str(filed)[:10])
                except ValueError:
                    continue
                if filed_d < cutoff:
                    continue
            acc = acc_col[idx] if idx < len(acc_col) else ""
            acc_nodash = acc.replace("-", "")
            doc = doc_col[idx] if idx < len(doc_col) else ""
            per_ticker.append(
                {
                    "id": f"sec:{cik10}:{acc}",
                    "source": "SEC-EDGAR",
                    "kind": "competitor",
                    "ticker": ticker,
                    "company": name,
                    "form": form,
                    "filed": filed,
                    "accession": acc,
                    "cik": cik10,
                    "url": (
                        f"https://www.sec.gov/Archives/edgar/data/"
                        f"{int(cik10)}/{acc_nodash}/{doc}"
                        if acc_nodash and doc
                        else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik10}"
                    ),
                }
            )
        per_ticker.sort(key=lambda r: r.get("filed") or "", reverse=True)
        out.extend(per_ticker[:max_per_ticker])
    out.sort(key=lambda r: r.get("filed") or "", reverse=True)
    return out


def inspect(ticker: str = "STNE") -> None:
    """One-shot check: resolve a ticker and print its latest filings' schema."""
    print(f"User-Agent: {_headers()['User-Agent'][:80]}...")
    ciks = resolve_ciks([ticker])
    print(f"{ticker} -> CIK {ciks}")
    if not ciks:
        print("Ticker not found in company_tickers.json — check spelling.")
        return
    cik10 = next(iter(ciks.values()))
    resp = requests.get(
        SUBMISSIONS_URL.format(cik10=cik10), headers=_headers(), timeout=60
    )
    print(f"HTTP {resp.status_code}  {resp.url}")
    resp.raise_for_status()
    recent = resp.json().get("filings", {}).get("recent", {})
    print("Columns:", list(recent.keys()))
    for i in range(min(5, len(recent.get("form", [])))):
        print(
            f"  {recent['filingDate'][i]}  {recent['form'][i]:6}  "
            f"{recent['accessionNumber'][i]}"
        )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect(sys.argv[2] if len(sys.argv) > 2 else "STNE")
    else:
        default = ["STNE", "PAGS", "NU", "INTR", "XP"]
        rows = fetch_filings(default, lookback_days=365)
        print(f"{len(rows)} filings across {len(default)} tickers (365d lookback)")
        for f in rows[:20]:
            print(
                f"  {f['filed']}  {f['ticker']:5} {f['form']:6} "
                f"{(f['company'] or '')[:40]}"
            )
