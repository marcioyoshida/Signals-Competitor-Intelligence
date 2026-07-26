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

Primary document bodies are fetched from Archives HTM/TXT (not PDF) so the
corpus and digests carry filing text, not only a link.

User-Agent (priority):
  1. ONCA_SEC_USER_AGENT env
  2. Module DEFAULT_USER_AGENT (set a real contact before production)

Lambda port: detect_new via DynamoDB; first run seeds baseline.
Content is enriched after diff so we only download bodies for new filings
plus a small digest context sample.
"""
from __future__ import annotations

import datetime as dt
import html as html_lib
import os
import re
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

# Pause between per-CIK / per-document requests (SEC asks for polite rate limiting).
REQUEST_PAUSE_SEC = 0.15

# Caps so Lambda digests and S3 objects stay bounded (20-F can be multi-MB).
MAX_RAW_CHARS = 100_000
MAX_SUBJECT_CHARS = 500
# Digests keep a short excerpt; full text lives on the raw S3 object.
MAX_DIGEST_TEXT_CHARS = 2_000

# Extensions we can turn into plain text. PDFs are noted, not parsed.
_TEXT_DOC_RE = re.compile(r"\.(htm|html|txt|xml)$", re.I)
_PDF_DOC_RE = re.compile(r"\.pdf$", re.I)
_TAG_RE = re.compile(r"<[^>]+>", re.S)
_WS_RE = re.compile(r"[ \t]+")
_BLANK_RE = re.compile(r"\n{3,}")


def _headers() -> dict[str, str]:
    ua = (os.environ.get("ONCA_SEC_USER_AGENT") or DEFAULT_USER_AGENT).strip()
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


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


def html_to_text(body: str) -> str:
    """Strip EDGAR HTML/SGML wrappers down to readable plain text."""
    if not body:
        return ""
    # Drop script/style blocks first.
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    # Prefer HTML body if present; else whole document (SGML wrappers still ok).
    m = re.search(r"(?is)<body[^>]*>(.*)</body>", text)
    if m:
        text = m.group(1)
    # Common EDGAR entities / non-breaking spaces before tag strip.
    text = text.replace("&nbsp;", " ").replace("\xa0", " ")
    # Block tags → paragraph breaks so soft-wrapped <P> lines rejoin cleanly.
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6]|li|table|section)[^>]*>", "\n", text)
    text = re.sub(r"(?i)<(p|div|h[1-6]|li|tr|table|section)[^>]*>", "\n", text)
    text = html_lib.unescape(_TAG_RE.sub(" ", text))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Within each block, collapse whitespace (EDGAR often hard-wraps mid-sentence).
    blocks: list[str] = []
    for block in text.split("\n"):
        collapsed = _WS_RE.sub(" ", block).strip()
        if collapsed:
            blocks.append(collapsed)
    text = "\n".join(blocks)
    return _BLANK_RE.sub("\n\n", text).strip()


def _subject_from_text(text: str, max_chars: int = MAX_SUBJECT_CHARS) -> str:
    """Substance after the SEC cover page — for digests/synthesis, not boilerplate."""
    if not text:
        return ""
    blob = re.sub(r"\s+", " ", text).strip()
    # Drop signature blocks (officers, dates) — not useful as subject.
    blob = re.split(r"\bSIGNATURES?\b", blob, maxsplit=1, flags=re.I)[0].strip()

    # Prefer explicit exhibit descriptions when present (common on 6-K cover sheets).
    exh = re.search(
        r"Exhibit\s+(?:No\.?\s*)?Description\s+(.+)$",
        blob,
        flags=re.I,
    )
    if exh:
        desc = re.sub(r"\s+", " ", exh.group(1)).strip()
        # Strip leading exhibit numbers like "99.1 "
        desc = re.sub(r"^[\d.]+\s*", "", desc).strip()
        if len(desc) >= 12:
            if len(desc) > max_chars:
                desc = desc[: max_chars - 1].rstrip() + "…"
            return desc

    # Skip cover-page checkbox / registrant header junk; keep the press body.
    cut = 0
    for pat in (
        r"(?i)Yes\s+No\s*\(\s*X\s*\)\s*",
        r"(?i)Indicate by check mark whether the registrant by furnishing"
        r".{0,200}?1934\.\s*",
        r"(?i)Indicate by check mark if the registrant is submitting the Form 6-K in paper"
        r".{0,80}",
        r"(?i)INCORPORATION BY REFERENCE\s+",
        r"(?i)EXHIBIT INDEX\s+",
    ):
        for m in re.finditer(pat, blob):
            # Only advance through the front matter (not deep into the body).
            if m.end() < max(500, int(len(blob) * 0.7)):
                cut = max(cut, m.end())
    body = blob[cut:].strip() if cut else blob
    if not body:
        body = blob
    if len(body) > max_chars:
        body = body[: max_chars - 1].rstrip() + "…"
    return body


def fetch_primary_text(
    url: str,
    *,
    max_chars: int = MAX_RAW_CHARS,
    timeout: int = 60,
) -> dict[str, Any]:
    """Download an Archives primary document and return plain text fields.

    Returns keys: text, subject, content_type, content_chars, content_error.
    PDFs and non-Archives URLs are skipped with a short error note (no crash).
    """
    result: dict[str, Any] = {
        "text": None,
        "subject": None,
        "content_type": None,
        "content_chars": 0,
        "content_error": None,
    }
    if not url or "sec.gov" not in url:
        result["content_error"] = "no_sec_url"
        return result
    if "Archives/edgar" not in url:
        result["content_error"] = "not_archives_primary"
        return result

    path = url.split("?", 1)[0]
    if _PDF_DOC_RE.search(path):
        result["content_error"] = "pdf_not_extracted"
        result["subject"] = "Primary document is PDF (text not extracted)"
        return result
    if not _TEXT_DOC_RE.search(path):
        # Unknown extension — still try if it looks like a filing file.
        if not path.lower().endswith((".htm", ".html", ".txt", ".xml")):
            result["content_error"] = "unsupported_primary_type"
            return result

    try:
        resp = requests.get(url, headers=_headers(), timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:  # pragma: no cover - network failures
        result["content_error"] = f"fetch_failed:{exc.__class__.__name__}"
        return result

    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    result["content_type"] = ctype or None
    raw = resp.content or b""
    # SEC sometimes serves HTML with a windows-1252 / utf-8 mix.
    body = raw.decode(resp.encoding or "utf-8", errors="replace")
    if len(body) > 5_000_000:
        body = body[:5_000_000]

    if "pdf" in (ctype or "") or body[:4] == "%PDF":
        result["content_error"] = "pdf_not_extracted"
        result["subject"] = "Primary document is PDF (text not extracted)"
        return result

    text = html_to_text(body)
    if not text:
        result["content_error"] = "empty_after_strip"
        return result

    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…[truncated]"
    result["text"] = text
    result["content_chars"] = len(text)
    result["subject"] = _subject_from_text(text) or None
    return result


def enrich_with_content(
    filings: list[dict[str, Any]],
    *,
    max_chars: int = MAX_RAW_CHARS,
    pause_sec: float = REQUEST_PAUSE_SEC,
    skip_existing: bool = True,
) -> list[dict[str, Any]]:
    """Fetch primary-document text for each filing; mutate and return the list.

    Sets: text, subject, content_chars, content_type, content_error (on failure).
    Skips rows that already have text when skip_existing is True.
    """
    first = True
    for filing in filings:
        if skip_existing and filing.get("text"):
            continue
        url = filing.get("url") or ""
        if not first:
            time.sleep(pause_sec)
        first = False
        got = fetch_primary_text(url, max_chars=max_chars)
        if got.get("text"):
            filing["text"] = got["text"]
            filing["subject"] = got.get("subject")
            filing["content_chars"] = got.get("content_chars") or len(got["text"])
            if got.get("content_type"):
                filing["content_type"] = got["content_type"]
            filing.pop("content_error", None)
        else:
            # Keep metadata-only row usable; surface why body is missing.
            if got.get("subject") and not filing.get("subject"):
                filing["subject"] = got["subject"]
            if got.get("content_error"):
                filing["content_error"] = got["content_error"]
    return filings


def fetch_filings(
    tickers: list[str],
    forms: set[str] | None = None,
    lookback_days: int | None = 365,
    max_per_ticker: int = 40,
    *,
    fetch_content: bool = False,
    content_max_chars: int = MAX_RAW_CHARS,
) -> list[dict[str, Any]]:
    """Fetch recent filings for the given tickers, filtered to `forms`.

    lookback_days: keep filings with filingDate on/after today - N days.
      None = no date filter (not recommended for first seed — hundreds of rows).
    max_per_ticker: cap after form+date filter (most-recent first).
    fetch_content: when True, also download primary-document text (slow;
      prefer enrich_with_content on the diffed subset in Lambda).
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
        desc_col = recent.get("primaryDocDescription", [])
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
            desc = desc_col[idx] if idx < len(desc_col) else ""
            # primaryDocDescription is often just "6-K"; only keep if informative.
            subject_hint = None
            if desc and desc.strip().upper() not in {
                str(form).upper(),
                "6-K",
                "20-F",
                "10-K",
                "10-Q",
                "8-K",
                "FORM 6-K",
                "FORM 20-F",
            }:
                subject_hint = desc.strip()
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
                    "primary_document": doc or None,
                    "subject": subject_hint,
                    "text": None,
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
    if fetch_content and out:
        enrich_with_content(out, max_chars=content_max_chars)
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
    elif len(sys.argv) > 1 and sys.argv[1] == "content":
        ticker = sys.argv[2] if len(sys.argv) > 2 else "NU"
        rows = fetch_filings([ticker], lookback_days=90, max_per_ticker=2)
        if not rows:
            print("No filings")
            sys.exit(1)
        enrich_with_content(rows[:1])
        r = rows[0]
        print(f"{r['filed']} {r['ticker']} {r['form']} chars={r.get('content_chars')}")
        print(f"url: {r['url']}")
        print(f"subject: {(r.get('subject') or '')[:200]}")
        print(f"text head:\n{(r.get('text') or '')[:800]}")
        if r.get("content_error"):
            print(f"error: {r['content_error']}")
    else:
        default = ["STNE", "PAGS", "NU", "INTR", "XP"]
        rows = fetch_filings(default, lookback_days=365)
        print(f"{len(rows)} filings across {len(default)} tickers (365d lookback)")
        for f in rows[:20]:
            print(
                f"  {f['filed']}  {f['ticker']:5} {f['form']:6} "
                f"{(f['company'] or '')[:40]}"
            )
