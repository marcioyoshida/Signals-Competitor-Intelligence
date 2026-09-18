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
import unicodedata
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable

import requests


def _fold(s: str) -> str:
    """Uppercase, accent-stripped form for robust phrase matching."""
    nfkd = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).upper()

RSS_URL = "https://news.google.com/rss/search"
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_HL, DEFAULT_GL, DEFAULT_CEID = "pt-BR", "BR", "BR:pt-419"

# Named outlets pulled directly (fuller coverage + direct publisher links, better
# citations than Google News redirects). Filtered to headlines naming a
# watchlisted competitor. (publisher, feed_url) — verified live 2026-08-16.
OUTLET_FEEDS: list[tuple[str, str]] = [
    ("Valor Econômico", "https://pox.globo.com/rss/valor/financas/"),
    ("Valor Econômico", "https://pox.globo.com/rss/valor/empresas/"),
    ("Money Times", "https://www.moneytimes.com.br/feed/"),
    ("Money Times", "https://www.moneytimes.com.br/tag/mercados/feed/"),
    # Mainstream financial press — previously reachable only through the Google
    # News aggregate, which yields redirect URLs instead of publisher links and
    # caps recall at the top 10 per query term. Verified live 2026-09-16.
    #
    # Exame: the *section* feed, deliberately — the site-wide one is general
    # interest (today's top item was an Oscars story). 25 items deep. NB it emits
    # ISO-8601 in <pubDate>, which is why `_iso` carries an ISO fallback.
    ("Exame", "https://exame.com/invest/feed/"),
    # InfoMoney: site-wide feed only. Its section feeds (/mercados/, /negocios/,
    # /economia/, /onde-investir/) all return HTTP 200 with ZERO items — empty
    # shells, re-probed 2026-09-16, don't retry them. So this one is low-yield:
    # 10 items deep and mostly general interest (lottery results, football), and
    # at 3 pipeline runs/day most of InfoMoney's output never lands inside that
    # window. Google News stays the primary route for this publisher; the direct
    # feed only upgrades citation quality on the subset it happens to catch.
    ("InfoMoney", "https://www.infomoney.com.br/feed/"),
    # Insurance trade press — the domain's real signal lives in specialist outlets,
    # so the news-dependent insurers (not separately B3-listed: SulAmérica,
    # Bradesco Seguros, Icatu) get corroborating distinct publishers here. Canonical
    # (post-redirect) feed URLs so no 301 hop is needed.
    ("CQCS", "https://cqcs.com.br/feed/"),
    ("Revista Apólice", "https://revistaapolice.com.br/feed/"),
    ("Revista Cobertura", "https://www.revistacobertura.com.br/feed/"),
    ("Sonho Seguro", "https://www.sonhoseguro.com.br/feed/"),
    # VC / PE / startup trade press — private-markets firms (Pátria, Vinci, Kinea,
    # Kaszek, Monashees, Igah) are private/foreign-listed with no CVM filing, so
    # their signal is episodic news; these outlets carry funding rounds and fund
    # closes, giving those entities distinct-publisher corroboration when news breaks.
    ("Brazil Journal", "https://braziljournal.com/feed/"),
    ("NeoFeed", "https://neofeed.com.br/feed/"),
    ("Startups", "https://startups.com.br/feed/"),
    ("Startupi", "https://startupi.com.br/feed/"),
    # Crypto & digital-assets trade press — the crypto module is news-only (BR
    # exchanges/asset managers don't file CVM material facts), so distinct-publisher
    # corroboration comes from these specialist outlets. Verified live 2026-08-19.
    ("Livecoins", "https://livecoins.com.br/feed/"),
    ("CriptoFácil", "https://www.criptofacil.com/feed/"),
    ("Cointelegraph Brasil", "https://cointelegraph.com.br/rss"),
    # Betting / iGaming trade press — the regulated bet operators (Betano, bet365,
    # Superbet, …) are news-only; this specialist outlet plus mainstream sports
    # coverage (via Google News) gives distinct-publisher corroboration. Verified
    # live 2026-08-19.
    ("iGaming Brazil", "https://igamingbrazil.com/feed/"),
    # Fundos Imobiliários / FIAGRO trade press — FII/FIAGRO news is ticker-centric
    # and abundant here; gives distinct-publisher corroboration for the fund
    # tickers. Verified live 2026-08-19.
    ("Funds Explorer", "https://www.fundsexplorer.com.br/feed"),
]

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
    # Crypto & digital-assets context — a "Mercado Bitcoin"/"Binance" headline
    # otherwise fails the finance gate (none of the above tokens appear). Stems
    # cover cripto/criptoativo/criptomoeda; the rest are single distinctive words.
    "cripto", "bitcoin", "blockchain", "token", "exchange", "stablecoin",
    "ethereum", "web3", "corretora", "custódia", "custodia", "tokeniza",
    # Consórcio context — likewise, an "Ademicon"/"Embracon" headline needs a
    # sector cue to pass. Stems: consorci(o/ado/os), contemplad(o/os/ção).
    "consorci", "consórci", "contemplad", "carta de créd", "carta de cred",
    # Betting / iGaming context — a "Betano"/"Superbet" headline (sponsorship,
    # GGR, SPA authorisation, market share) needs a sector cue. "aposta" stem
    # covers aposta(s)/apostador; the rest are distinctive single words.
    "aposta", "cassino", "casino", "igaming", "sportsbook", "loteria",
    "bookmaker", "bet.br", "ggr",
    # Fund context (FII / FIAGRO) — a "MXRF11"/"KNRI11" headline needs a sector
    # cue. "imobili" covers imobiliário/-a/-os; "rendiment" the monthly yield;
    # "cota"/"cotista"; "alugu" aluguel/aluguéis. "fii"/"fiagro" standalone.
    "fii", "fiagro", "cota", "rendiment", "imobili", "alugu",
    # Acquiring / maquininhas context — a "Cielo"/"Getnet"/"Rede" headline about
    # merchant acquiring needs a sector cue beyond the generic "pagament"/"cartão".
    # "adquir" covers adquirência/adquirente; "credenciad" credenciadora; MDR/TPV
    # are the industry's KPIs.
    "adquir", "maquininha", "credenciad", "mdr", "tpv",
})

# Terms are word-STEM prefixes ("pagament" -> "pagamentos"), matched at a word
# start only. Anchoring the left boundary stops a term matching mid-word — e.g.
# "ação" (share) must NOT match inside "celebração" (celebration), the exact
# false positive that let a music headline through.
_FINANCE_RE = re.compile(
    r"(?<![0-9a-zà-ÿ])(?:" + "|".join(re.escape(k) for k in FINANCE_TERMS) + r")"
)


def _has_finance_context(text: str) -> bool:
    return bool(_FINANCE_RE.search((text or "").lower()))


def _phrase_match(term_folded: str, title_folded: str) -> bool:
    """Whole-phrase match: `term_folded` must occur in `title_folded` with a
    non-alphanumeric (or string-edge) boundary on both sides.

    Plain substring containment lets a short name swallow a longer word it's a
    prefix of — e.g. the entity "Invest" matching inside "INVESTIMENTOS", a
    routine word in Brazilian financial press. `_fold()` already strips accents
    to plain ASCII, so a simple `.isalnum()` boundary check (no regex needed)
    is exact here, unlike `_FINANCE_RE`'s accent-aware character class.
    """
    if not term_folded:
        return False
    start = 0
    n = len(term_folded)
    while True:
        idx = title_folded.find(term_folded, start)
        if idx == -1:
            return False
        before_ok = idx == 0 or not title_folded[idx - 1].isalnum()
        after = idx + n
        after_ok = after == len(title_folded) or not title_folded[after].isalnum()
        if before_ok and after_ok:
            return True
        start = idx + 1


def fetch_news(
    terms: Iterable[str],
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    *,
    max_per_term: int = 10,
    require_finance_context: bool = True,
    include_outlets: bool = True,
    outlet_feeds: list[tuple[str, str]] | None = None,
    today: dt.date | None = None,
    fetcher: Callable[[str], bytes] | None = None,
    outlet_fetcher: Callable[[str], bytes] | None = None,
    pause_sec: float = 0.3,
    max_terms: int = 80,
) -> list[dict[str, Any]]:
    """Recent headlines mentioning a competitor in the title (higher precision)."""
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=lookback_days)
    fetch = fetcher or _fetch_rss
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Full deduped set drives the (cheap, bounded) outlet-feed matching so EVERY
    # tracked entity gets outlet coverage; the expensive one-HTTP-per-term Google
    # News loop is capped at max_terms (registry-scale safety, budget-bounded).
    uniq_all = [t for t in dict.fromkeys(str(t).strip() for t in terms) if t]
    uniq = uniq_all[:max_terms]
    for term in uniq:
        term_f = _fold(term)
        kept = 0
        for rec in _parse(fetch(term), term):
            if kept >= max_per_term:
                break
            date = _parse_date(rec.get("date"))
            if not date or date < cutoff:
                continue
            title = rec.get("title") or ""
            # precision: the competitor must appear in the headline as a phrase
            # (full brand, accent-folded, word-bounded) — not just a shared
            # generic token, and not as a prefix swallowed by a longer word.
            if term_f and not _phrase_match(term_f, _fold(title)):
                continue
            # and it must be business news (drops band/stadium/culture noise)
            if require_finance_context and not _has_finance_context(title):
                continue
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            out.append(rec)
            kept += 1
        if pause_sec:
            time.sleep(pause_sec)

    # Named outlets pulled directly, filtered to headlines naming a competitor.
    if include_outlets:
        ofetch = outlet_fetcher or _fetch_url
        term_folded = {t: _fold(t) for t in uniq_all}
        for publisher, feed_url in (outlet_feeds if outlet_feeds is not None else OUTLET_FEEDS):
            for rec in _parse_feed(ofetch(feed_url), publisher):
                title_f = _fold(rec.get("title") or "")
                matched = next(
                    (t for t, tf in term_folded.items() if tf and _phrase_match(tf, title_f)),
                    None,
                )
                if not matched:
                    continue
                date = _parse_date(rec.get("date"))
                if not date or date < cutoff:
                    continue
                if require_finance_context and not _has_finance_context(
                    rec.get("title") or ""
                ):
                    continue
                if rec["id"] in seen:
                    continue
                rec["company"] = matched
                rec["name"] = matched
                seen.add(rec["id"])
                out.append(rec)

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


def _fetch_url(url: str) -> bytes:
    try:
        resp = requests.get(url, timeout=25, headers={"User-Agent": "Onca-CI/1.0 (competitive-intelligence)"})
        return resp.content if resp.status_code == 200 else b""
    except Exception as exc:  # pragma: no cover - upstream best-effort
        print(f"Warning: outlet feed failed for {url}: {exc}")
        return b""


def _parse_feed(content: bytes, publisher: str) -> list[dict[str, Any]]:
    """Parse a standard outlet RSS feed (fixed publisher, direct link)."""
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
        sig = re.sub(r"[^a-z0-9]+", "", f"{title}|{publisher}".lower())
        out.append(
            {
                "id": "news:" + hashlib.sha1(sig.encode()).hexdigest()[:16],
                "source": "News",
                "kind": "competitor",
                "publisher": publisher,
                "title": title,
                "subject": title,
                "company": None,  # set by the caller once a term matches the title
                "name": None,
                "date": _iso(it.findtext("pubDate")),
                "url": link,  # direct publisher URL
            }
        )
    return out


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
    """Normalize a feed's pubDate to ISO ``YYYY-MM-DD``.

    RSS nominally mandates RFC-822 and 16 of the 17 outlet feeds comply, but Exame
    emits ISO-8601 in ``<pubDate>``. ``parsedate_to_datetime`` rejects that, and an
    empty return here is indistinguishable from "no date" downstream — the item is
    dropped by the cutoff check in ``fetch_news`` with no error. Falling back keeps
    a whole publisher from vanishing silently.
    """
    s = str(pubdate or "").strip()
    if not s:
        return ""
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        pass
    try:
        return dt.datetime.fromisoformat(s).date().isoformat()
    except ValueError:
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
