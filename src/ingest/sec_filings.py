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

Field names here follow EDGAR's documented submissions schema, but this
was not verifiable from the build sandbox — run inspect() against the
live API before relying on it. Lambda port: JsonState → DynamoDB.
"""
from __future__ import annotations

from typing import Any

import requests

# SEC requires a real contact in the UA. REPLACE before running.
HEADERS = {"User-Agent": "Onca CI research (contact: REPLACE_WITH_YOUR_EMAIL)"}

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"

# Forms worth surfacing. FPIs use 20-F / 6-K; domestic filers 10-K/10-Q/8-K.
DEFAULT_FORMS = {"20-F", "6-K", "10-K", "10-Q", "8-K", "F-1", "424B4"}


def resolve_ciks(tickers: list[str]) -> dict[str, str]:
    """Map tickers (e.g. STNE) to zero-padded 10-digit CIKs via EDGAR."""
    resp = requests.get(TICKERS_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    want = {t.upper() for t in tickers}
    out: dict[str, str] = {}
    for row in resp.json().values():
        tk = str(row.get("ticker", "")).upper()
        if tk in want:
            out[tk] = str(row["cik_str"]).zfill(10)
    return out


def fetch_filings(
    tickers: list[str], forms: set[str] | None = None
) -> list[dict[str, Any]]:
    """Fetch recent filings for the given tickers, filtered to `forms`."""
    forms = forms or DEFAULT_FORMS
    ciks = resolve_ciks(tickers)
    out: list[dict[str, Any]] = []
    for ticker, cik10 in ciks.items():
        resp = requests.get(
            SUBMISSIONS_URL.format(cik10=cik10), headers=HEADERS, timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        name = data.get("name")
        recent = data.get("filings", {}).get("recent", {})
        # 'recent' is columnar: parallel arrays. Zip by index.
        forms_col = recent.get("form", [])
        dates_col = recent.get("filingDate", [])
        acc_col = recent.get("accessionNumber", [])
        doc_col = recent.get("primaryDocument", [])
        for i, form in enumerate(forms_col):
            if form not in forms:
                continue
            acc = acc_col[i] if i < len(acc_col) else ""
            acc_nodash = acc.replace("-", "")
            doc = doc_col[i] if i < len(doc_col) else ""
            out.append(
                {
                    "id": f"sec:{cik10}:{acc}",
                    "source": "SEC-EDGAR",
                    "kind": "competitor",
                    "ticker": ticker,
                    "company": name,
                    "form": form,
                    "filed": dates_col[i] if i < len(dates_col) else None,
                    "accession": acc,
                    "url": (
                        f"https://www.sec.gov/Archives/edgar/data/"
                        f"{int(cik10)}/{acc_nodash}/{doc}"
                    ),
                }
            )
    return out


def inspect(ticker: str = "STNE") -> None:
    """One-shot check: resolve a ticker and print its latest filings' schema."""
    ciks = resolve_ciks([ticker])
    print(f"{ticker} -> CIK {ciks}")
    if not ciks:
        print("Ticker not found in company_tickers.json — check spelling.")
        return
    cik10 = next(iter(ciks.values()))
    resp = requests.get(SUBMISSIONS_URL.format(cik10=cik10), headers=HEADERS, timeout=60)
    print(f"HTTP {resp.status_code}  {resp.url}")
    resp.raise_for_status()
    recent = resp.json().get("filings", {}).get("recent", {})
    print("Columns:", list(recent.keys()))
    for i in range(min(5, len(recent.get("form", [])))):
        print(f"  {recent['filingDate'][i]}  {recent['form'][i]:6}  {recent['accessionNumber'][i]}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect(sys.argv[2] if len(sys.argv) > 2 else "STNE")
    else:
        default = ["STNE", "PAGS", "NU", "INTR", "XP"]
        rows = fetch_filings(default)
        print(f"{len(rows)} filings across {len(default)} tickers")
        for f in sorted(rows, key=lambda x: x["filed"] or "", reverse=True)[:20]:
            print(f"  {f['filed']}  {f['ticker']:5} {f['form']:6} {f['company']}")
