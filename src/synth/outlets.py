"""One display name per news outlet.

Google News gives the same outlet as "O GLOBO" on one item and "oglobo.globo.com" on the next
(its <source> text is the bare domain when the publisher sent no name). The feed showed both
spellings for 55 outlets on 10-04 (Seu Dinheiro 24 vs seudinheiro.com 25), which reads as two
sources and splits outlet counts. Names are LEARNED from the data — the most frequent
non-domain spelling under the same outlet key — never typed in by hand; an outlet only ever
seen as a domain keeps its domain (minus "www.")."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Iterable

_GENERIC_LABELS = {"www", "br", "pt", "en", "es", "m", "amp", "mobile", "portal", "blog", "news",
                   "noticias", "app", "site"}
_DOMAIN = re.compile(r"(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")


def _fold(s: Any) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def is_domain(name: Any) -> bool:
    return bool(_DOMAIN.fullmatch(str(name or "").strip().lower()))


def outlet_key(name: Any, url: Any = None) -> str:
    """"O Dia" and "odia.ig.com.br" collapse to ``odia`` (first label of a domain)."""
    pub = _fold(name).strip()
    if not pub:
        m = re.search(r"https?://([^/]+)/?", str(url or ""))
        pub = (m.group(1) if m else "").lower()
    pub = re.sub(r"^www\.", "", pub)
    if _DOMAIN.fullmatch(pub):
        # first label that names the site: "br.investing.com" is investing, not "br" (which
        # would merge it with br.tradingview.com); "portal.x.sp.gov.br" is x
        labels = pub.split(".")
        pub = next((lb for lb in labels[:-1] if lb not in _GENERIC_LABELS), labels[0])
    return re.sub(r"[^a-z0-9]+", "", pub) or "?"


def learn(names: Iterable[Any]) -> dict[str, str]:
    """{outlet_key: preferred display name} from every spelling seen."""
    seen: dict[str, Counter] = defaultdict(Counter)
    for n in names:
        n = str(n or "").strip()
        if n:
            seen[outlet_key(n)][n] += 1
    out = {}
    for key, c in seen.items():
        named = [(cnt, n) for n, cnt in c.items() if not is_domain(n)]
        if named:
            out[key] = max(named)[1]
    return out


def display(name: Any, names: dict[str, str]) -> Any:
    """The learned name for a domain-style spelling; any other spelling is kept as written."""
    if not is_domain(name):
        return name
    return names.get(outlet_key(name)) or re.sub(r"^www\.", "", str(name).strip())  # never another domain


# Fields that hold an outlet name next to a URL across the feed: narrative citations
# (``label``), news sources (``publisher``), sector-event outlet lists.
def _walk(o: Any, visit) -> None:
    if isinstance(o, dict):
        visit(o)
        for v in o.values():
            _walk(v, visit)
    elif isinstance(o, list):
        for v in o:
            _walk(v, visit)


def _news_name_fields(d: dict[str, Any]) -> list[str]:
    if d.get("kind") == "official":
        return []
    url = str(d.get("url") or "")
    fields = []
    if "publisher" in d and isinstance(d.get("publisher"), str):
        fields.append("publisher")
    if "label" in d and isinstance(d.get("label"), str) and url.startswith("http"):
        fields.append("label")
    return fields


def name_in_place(obj: Any, names: dict[str, str] | None = None) -> int:
    """Rewrite domain-style outlet names inside ``obj`` (a feed, a store) to the learned name.
    Learns from ``obj`` itself unless ``names`` is given. Returns how many fields changed."""
    if names is None:
        seen: list[str] = []
        _walk(obj, lambda d: seen.extend(d[f] for f in _news_name_fields(d)))
        names = learn(seen)
    n = 0

    def fix(d: dict[str, Any]) -> None:
        nonlocal n
        for f in _news_name_fields(d):
            new = display(d[f], names)
            if new != d[f]:
                d[f] = new
                n += 1
        if isinstance(d.get("outlets"), list) and d.get("kind") == "sector_event":
            d["outlets"] = [display(o, names) for o in d["outlets"]]
    _walk(obj, fix)
    return n
