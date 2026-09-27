"""Ingest CVM normative acts + enforcement news (issue #194, audit R5, fix #7).

The 2026-09-27 regulator-coverage audit (``docs/2026-09-27-regulator-coverage-audit.md``)
found **no CVM normative or enforcement source at all**: the R$ 203 mi Banco Master PAS
fine, the Ofício Circular CVM/SSE 3/2026 (FIDC performance fee) and the OPEA
Securitizadora CRI stop order all reached Onça only as thin, uncorroborated news, never
as the regulator's own act (R5, B1/B2/B3).

**ADR-0003 admission (government source, zero-risk tier — see ``docs/CONTEXT.md``
"Legal tiering"):**
  - ``conteudo.cvm.gov.br`` — the CVM's own OpenCms site, publishing signed RSS feeds
    under ``/feed/*.xml`` (legislação, sancionadores, despachos, decisões). Live-checked
    2026-09-27: HTTP 200, no login, no robots.txt disallow on ``/feed/`` or ``/legislacao/``.
  - ``www.gov.br/cvm`` — the CVM's press-office listing (Plone gov.br platform). Live
    robots.txt (``https://www.gov.br/robots.txt``) allows ``User-Agent: *`` here (only a
    handful of unrelated paths are disallowed); logged-out GET, no auth, no CAPTCHA.
  Both are the regulator's own primary publication — the same tier as ``bcb_normativos``
  and ``dou.py``, not a scrape of a third party.

**Two lenses:**

1. :func:`fetch_legislacao` — the ``conteudo.cvm.gov.br/feed/legislacao.xml`` RSS: new
   Resoluções, Instruções, Ofícios-Circulares, Deliberações, Notas Explicativas, Pareceres
   de Orientação. This is the feed that (live-checked 2026-09-27) already carries "Ofício
   Circular CVM/SSE 03/26, de 11 de Setembro de 2026" — exactly the B2 audit miss.
2. :func:`fetch_noticias` — the ``gov.br/cvm`` notícias listing (no RSS item covers
   sanctions/stop-orders content, so this is scraped): PAS judgments (tagged "ATIVIDADE
   SANCIONADORA"), SRE stop/reversal orders ("suspensa oferta…", "revertida suspensão…"),
   and CVM's own Ofício-Circular announcements. A relevance keyword filter keeps the
   enforcement/normative items and drops routine agenda/HR notices. For each NEW item the
   detail page is fetched for the full article body (``id="parent-fieldname-text"``), so
   the KB document is quotable — not a name+CNPJ stub (the #194 audit's R12 pitfall).

Records: ``kind: "regulatory"``, ``source: "CVM"``, ``organ: "Comissão de Valores
Mobiliários"``, ``doc_type``, ``title``, ISO ``date``, ``url``, full/best-available
``text``. Fed through ``federal_acts.annotate`` (same as ``bcb_normativos``) for
industries + severity.

Best-effort: any HTTP/parse failure degrades to ``[]`` for that lens; never fabricates a
value — a field that can't be read stays ``None``.
"""
from __future__ import annotations

import datetime as dt
import html as _html
import re
from typing import Any

import requests

LEGISLACAO_FEED_URL = "https://conteudo.cvm.gov.br/feed/legislacao.xml"
NOTICIAS_LISTING_URL = "https://www.gov.br/cvm/pt-br/assuntos/noticias"
ORGAN = "Comissão de Valores Mobiliários"
USER_AGENT = "Onca-CI/1.0 (competitive-intelligence; regulatory monitoring)"
TIMEOUT = 30

_MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}

# Legislação RSS <title> prefixes -> a normalized doc_type.
_LEGIS_TYPE_RE = re.compile(
    r"^(Resolução CVM|Resolução Conjunta CVM/\w+|Instrução CVM|Instrução Normativa CVM|"
    r"Ofício[\s-]Circular(?:\sAnual)? CVM(?:/[\w-]+)?|Deliberação CVM|Nota Explicativa(?: à Instrução)?|"
    r"Parecer de Orientação(?: CVM)?|Ato Declaratório(?: CVM)?|Portaria(?: Conjunta)? CVM)",
    re.I,
)
_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_TAG_RE = re.compile(r"<([a-zA-Z:]+)[^>]*>(.*?)</\1>", re.S)
_RFC822_RE = re.compile(
    r"(\d{1,2}) (\w{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2})"
)
_MONTHS_EN = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
_DOU_DATE_RE = re.compile(r"Publicad[ao] no DOU de (\d{2})\.(\d{2})\.(\d{4})")

# Enforcement/normative-announcement keywords for the notícias listing (R5 B1/B2/B3):
# PAS judgments ("multa", "sanção", "julgamento", "PAS"), SRE stop/reversal orders
# ("suspens", "revertid"), and CVM's own normative announcements ("ofício circular",
# "área técnica"). Deliberately excludes routine agenda/travel/HR notices.
_RELEVANT_NEWS_RE = re.compile(
    r"multa|san[çc][aã]o|julgamento|\bPAS\b|processo administrativo|termo de compromisso|"
    r"suspens|revertid|revoga|cassa|ofício[\s-]circular|área técnica|oferta p[uú]blica",
    re.I,
)
_SANCTION_CATEGORY_RE = re.compile(r"ATIVIDADE SANCIONADORA", re.I)


_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


def _strip_cdata(text: str) -> str:
    """RSS often wraps title/description in ``<![CDATA[...]]>``; unwrap before tag-stripping
    (a naive ``<[^>]+>`` strip would otherwise treat the whole CDATA span as one giant tag)."""
    m = _CDATA_RE.search(text or "")
    return m.group(1) if m else (text or "")


def _clean_html(text: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", " ", _strip_cdata(text))).strip()


def _iso_rfc822(pubdate: str) -> str | None:
    """"Tue, 15 Sep 2026 18:19:08 -0300" -> "2026-09-15"."""
    m = _RFC822_RE.search(pubdate or "")
    if not m:
        return None
    day, mon, year = m.group(1), m.group(2), m.group(3)
    month = _MONTHS_EN.get(mon)
    if not month:
        return None
    try:
        return dt.date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def _iso_br_date(s: str) -> str | None:
    """"16/09/2026" -> "2026-09-16"."""
    m = re.match(r"^\s*(\d{2})/(\d{2})/(\d{4})\s*$", s or "")
    if not m:
        return None
    d, mo, y = m.groups()
    try:
        return dt.date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def _slug_from_url(url: str) -> str:
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


# --- lens 1: legislação RSS ------------------------------------------------------------------

def _parse_legislacao_item(block: str) -> dict[str, Any] | None:
    fields: dict[str, str] = {}
    for m in _TAG_RE.finditer(block):
        tag, val = m.group(1), m.group(2)
        fields.setdefault(tag, val)
    title = _clean_html(fields.get("title", ""))
    link = _clean_html(fields.get("link", ""))
    if not title or not link:
        return None
    description = fields.get("description", "")
    summary = _clean_html(description)
    doc_type_m = _LEGIS_TYPE_RE.match(title)
    doc_type = doc_type_m.group(1) if doc_type_m else "Ato normativo"
    # Prefer the DOU publication date embedded in the description over the feed's own
    # pubDate (which is when the CVM site last touched the page, sometimes later).
    dou_date_m = _DOU_DATE_RE.search(description)
    if dou_date_m:
        d, mo, y = dou_date_m.groups()
        try:
            date = dt.date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            date = None
    else:
        date = None
    if not date:
        date = _iso_rfc822(fields.get("pubDate", ""))
    return {
        "id": f"cvm-legis:{_slug_from_url(link)}",
        "source": "CVM",
        "kind": "regulatory",
        "organ": ORGAN,
        "doc_type": doc_type,
        "title": title,
        "subject": title,
        "text": summary,
        "date": date,
        "url": link,
    }


def fetch_legislacao(
    lookback_days: int = 30,
    *,
    fetcher: Any = None,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """New Resoluções/Instruções/Ofícios-Circulares from the CVM's own RSS feed."""
    get = fetcher or _default_get
    try:
        body = get(LEGISLACAO_FEED_URL)
    except Exception as exc:  # pragma: no cover - upstream best-effort
        print(f"Warning: CVM legislação feed fetch failed: {exc}")
        return []
    if not body:
        return []
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=lookback_days)
    out: list[dict[str, Any]] = []
    for block in _ITEM_RE.findall(body):
        rec = _parse_legislacao_item(block)
        if not rec:
            continue
        if rec["date"] and rec["date"] < cutoff.isoformat():
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    return out


# --- lens 2: gov.br/cvm notícias listing (PAS/stop-orders/enforcement) -----------------------

_LISTING_ITEM_RE = re.compile(
    r'<div class="subtitulo-noticia">(?P<cat>.*?)</div>.*?'
    r'<h2 class="titulo">\s*<a href="(?P<url>[^"]+)">(?P<title>.*?)</a>\s*</h2>.*?'
    r'<span class="data">\s*(?P<date>[\d/]+)\s*</span>\s*<span> - </span>\s*(?P<summary>.*?)\s*</span>',
    re.S,
)
_ARTICLE_BODY_RE = re.compile(
    r'id="parent-fieldname-text"[^>]*>\s*<div[^>]*>(.*?)</div>\s*(?:<div|</div>\s*</div>\s*<div id="viewlet)',
    re.S,
)


def _parse_listing(html_body: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in _LISTING_ITEM_RE.finditer(html_body or ""):
        title = _clean_html(m.group("title"))
        url = m.group("url").strip()
        category = _clean_html(m.group("cat"))
        summary = _clean_html(m.group("summary"))
        date = _iso_br_date(m.group("date"))
        if not title or not url:
            continue
        out.append({
            "title": title, "url": url, "category": category,
            "summary": summary, "date": date,
        })
    return out


def _is_relevant_news(item: dict[str, Any]) -> bool:
    return bool(
        _SANCTION_CATEGORY_RE.search(item.get("category") or "")
        or _RELEVANT_NEWS_RE.search(item.get("title") or "")
    )


def _news_doc_type(item: dict[str, Any]) -> str:
    title = item.get("title") or ""
    if _SANCTION_CATEGORY_RE.search(item.get("category") or "") or re.search(
        r"multa|julgamento|\bPAS\b", title, re.I
    ):
        return "PAS (julgamento)"
    if re.search(r"suspens", title, re.I):
        return "Suspensão de oferta (SRE)"
    if re.search(r"revertid", title, re.I):
        return "Reversão de suspensão (SRE)"
    if re.search(r"ofício[\s-]circular|área técnica", title, re.I):
        return "Ofício-Circular / orientação técnica"
    return "Notícia CVM"


def fetch_article_text(url: str, *, fetcher: Any = None) -> str | None:
    """Full article body from a gov.br/cvm notícia page (``id="parent-fieldname-text"``)."""
    get = fetcher or _default_get
    try:
        body = get(url)
    except Exception as exc:  # pragma: no cover - upstream best-effort
        print(f"Warning: CVM notícia article fetch failed for {url}: {exc}")
        return None
    if not body:
        return None
    m = _ARTICLE_BODY_RE.search(body)
    if not m:
        return None
    return _clean_html(m.group(1)) or None


def fetch_noticias(
    lookback_days: int = 30,
    *,
    fetcher: Any = None,
    fetch_full_text: bool = True,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """PAS judgments, SRE stop orders, and Ofício-Circular announcements from gov.br/cvm."""
    get = fetcher or _default_get
    try:
        body = get(NOTICIAS_LISTING_URL)
    except Exception as exc:  # pragma: no cover - upstream best-effort
        print(f"Warning: CVM notícias listing fetch failed: {exc}")
        return []
    if not body:
        return []
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=lookback_days)
    out: list[dict[str, Any]] = []
    for item in _parse_listing(body):
        if not _is_relevant_news(item):
            continue
        if item["date"] and item["date"] < cutoff.isoformat():
            continue
        text = item["summary"]
        if fetch_full_text:
            full = fetch_article_text(item["url"], fetcher=get)
            if full:
                text = full
        out.append({
            "id": f"cvm-noticia:{_slug_from_url(item['url'])}",
            "source": "CVM",
            "kind": "regulatory",
            "organ": ORGAN,
            "doc_type": _news_doc_type(item),
            "title": item["title"],
            "subject": item["title"],
            "text": text,
            "date": item["date"],
            "url": item["url"],
        })
    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    return out


def fetch_recent(
    lookback_days: int = 30,
    *,
    fetcher: Any = None,
    fetch_full_text: bool = True,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Both lenses combined, deduped by id, newest first."""
    legis = fetch_legislacao(lookback_days, fetcher=fetcher, today=today)
    news = fetch_noticias(
        lookback_days, fetcher=fetcher, fetch_full_text=fetch_full_text, today=today
    )
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for rec in legis + news:
        if rec["id"] in seen:
            continue
        seen.add(rec["id"])
        out.append(rec)
    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    return out


def _default_get(url: str) -> str:
    resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    return resp.text if resp.status_code == 200 else ""


def inspect(lookback_days: int = 45) -> None:
    legis = fetch_legislacao(lookback_days)
    print(f"legislação: {len(legis)} acts (last {lookback_days}d)")
    for r in legis[:10]:
        print(f"  [{r['date']}] {r['doc_type']:35} {r['title'][:70]}")
        print(f"    {r['url']}")
    news = fetch_noticias(lookback_days, fetch_full_text=False)
    print(f"\nnotícias (enforcement/normative filter): {len(news)} items (last {lookback_days}d)")
    for r in news[:10]:
        print(f"  [{r['date']}] {r['doc_type']:35} {r['title'][:70]}")
        print(f"    {r['url']}")


if __name__ == "__main__":
    inspect()
