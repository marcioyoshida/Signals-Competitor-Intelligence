"""Ingest trade-press headlines about competitors — qualitative "why" signal.

Legal posture: consume a **syndication feed** (Google News RSS), keeping only
headline + publisher + link. We never scrape or reproduce article bodies (many
outlets are paywalled) — the citation is the link back to the publisher. Google
News RSS aggregates all Brazilian outlets (Valor, Brazil Journal, NeoFeed,
InfoMoney, Exame, …) for one query, so no per-outlet feed maintenance.

Schema verified live 2026-08-16: RSS item.{title, link, pubDate (RFC822),
source (publisher)}. News is lower-authority than official filings, so the
`news` lens carries a lower strategic weight; every item still links to its
source, consistent with the cited-intel positioning.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable

import requests

RSS_URL = "https://news.google.com/rss/search"
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_HL, DEFAULT_GL, DEFAULT_CEID = "pt-BR", "BR", "BR:pt-419"

# Ambiguous single-word brands (Stone, Nubank, Inter) pull band/stadium/culture
# noise. Require a finance-context term in the headline to keep it business news.
FINANCE_TERMS = frozenset({
    "banco", "fintech", "pagament", "pix", "credito", "crédito", "emprest",
    "emprést", "lucro", "prejuíz", "prejuiz", "resultado", "receita", "ação",
    "ações", "acoes", "bolsa", "b3", "aquisi", "fusão", "fusao", "ipo", "oferta",
    "cvm", "juros", "cartão", "cartao", "investiment", "seguro", "balanço",
    "balanco", "dividend", "capital", "valuation", "preço-alvo", "preco-alvo",
    "fraude", "golpe", "aporte", "rodada", "funding", "ceo", "cfo", "expansã",
    "expansao", "digital", "unicórnio", "unicornio", "financeir", "títulos",
    "titulos", "debênture", "debenture", "fundo", "susep", "cade", "bacen",
})


def fetch_news(
    terms: Iterable[str],
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    *,
    max_per_term: int = 10,
    require_finance_context: bool = True,
    today: dt.date | None = None,
    fetcher: Callable[[str], bytes] | None = None,
    pause_sec: float = 0.3,
    max_terms: int = 25,
) -> list[dict[str, Any]]:
    """Recent headlines mentioning a competitor in the title (higher precision)."""
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=lookback_days)
    fetch = fetcher or _fetch_rss
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    uniq = [t for t in dict.fromkeys(str(t).strip() for t in terms) if t][:max_terms]
    for term in uniq:
        tokens = [w for w in re.split(r"\W+", term.upper()) if len(w) >= 3]
        kept = 0
        for rec in _parse(fetch(term), term):
            if kept >= max_per_term:
                break
            date = _parse_date(rec.get("date"))
            if not date or date < cutoff:
                continue
            title = rec.get("title") or ""
            title_up = title.upper()
            # precision: the competitor must be in the headline, not just the body
            if tokens and not any(tok in title_up for tok in tokens):
                continue
            # and it must be business news (drops band/stadium/culture noise)
            if require_finance_context:
                low = title.lower()
                if not any(k in low for k in FINANCE_TERMS):
                    continue
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            out.append(rec)
            kept += 1
        if pause_sec:
            time.sleep(pause_sec)
    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    return out


def _fetch_rss(term: str) -> bytes:
    try:
        resp = requests.get(
            RSS_URL,
            params={"q": f'"{term}"', "hl": DEFAULT_HL, "gl": DEFAULT_GL, "ceid": DEFAULT_CEID},
            timeout=25,
            headers={"User-Agent": "Onca-CI/1.0 (competitive-intelligence)"},
        )
        return resp.content if resp.status_code == 200 else b""
    except Exception as exc:  # pragma: no cover - upstream best-effort
        print(f"Warning: news fetch failed for {term}: {exc}")
        return b""


def _parse(content: bytes, term: str) -> list[dict[str, Any]]:
    if not content:
        return []
    try:
        root = ET.fromstring(content)
    except Exception:  # pragma: no cover - feed fragility
        return []
    out: list[dict[str, Any]] = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        src_el = it.find("source")
        publisher = (src_el.text.strip() if src_el is not None and src_el.text else None)
        # Google News often appends " - Publisher" to the title; trim it.
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -(len(publisher) + 3)].strip()
        # Stable id from headline + publisher (link carries volatile tracking).
        sig = re.sub(r"[^a-z0-9]+", "", f"{title}|{publisher or ''}".lower())
        out.append(
            {
                "id": "news:" + hashlib.sha1(sig.encode()).hexdigest()[:16],
                "source": "News",
                "kind": "competitor",
                "publisher": publisher or "imprensa",
                "title": title,
                "subject": title,
                "company": term,  # matched term drives entity resolution
                "name": term,
                "date": _iso(it.findtext("pubDate")),
                "url": link,
            }
        )
    return out


def _iso(pubdate: Any) -> str:
    try:
        return parsedate_to_datetime(str(pubdate)).date().isoformat()
    except Exception:
        return ""


def _parse_date(value: Any) -> dt.date | None:
    s = (str(value) if value is not None else "").strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return dt.date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def inspect(terms: list[str] | None = None, lookback_days: int = 14) -> None:
    news = fetch_news(terms or ["BTG Pactual", "Nubank"], lookback_days=lookback_days)
    print(f"fetch_news: {len(news)} headlines")
    for r in news[:15]:
        print(f"  [{r['date']}] {r['publisher'][:22]:22} {r['title'][:60]}")


if __name__ == "__main__":
    import sys

    inspect(sys.argv[1:] or None)
