"""Ingest Diário Oficial da União (DOU) acts mentioning tracked competitors.

The DOU is where BCB authorization atos, SUSEP acts, CADE (antitrust) decisions,
appointments and sanctions are *officially* published — so one keyword-filtered
DOU feed covers all three regulators via a single reliable source (CADE's and
SUSEP's own open-data endpoints are currently 401/404). Authoritative, citable.

Source: Imprensa Nacional search (in.gov.br/consulta/-/buscar/dou). Results are
embedded as JSON in the page's ``*_params`` script tag. Schema verified live
2026-08-16: jsonArray[].{pubName, urlTitle, title, content, pubDate (DD/MM/YYYY),
artType, hierarchyStr}.

Signal: detect_new on stable act ids (urlTitle carries a unique suffix);
first run seeds baseline. Best-effort scrape — degrades to [] on any parse issue.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from typing import Any, Callable, Iterable

import requests

SEARCH_URL = "https://www.in.gov.br/consulta/-/buscar/dou"
ACT_URL = "https://www.in.gov.br/web/dou/-/{slug}"
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_SECTIONS = ("do1",)  # Seção 1 = normative/regulatory acts
_PARAMS_RE = re.compile(r'id="_[^"]*_params"[^>]*>\s*(\{.*?\})\s*</script>', re.S)

# Keep only acts from intel-relevant regulators (matched in hierarchyStr) — the
# DOU otherwise returns tax judgments (CARF / Receita), sports incentives, and
# regional councils that merely mention a bank's name. Empty tuple = keep all.
RELEVANT_ORGANS = (
    "Superintendência de Seguros Privados",  # SUSEP (insurance)
    "Defesa Econômica",                      # CADE (antitrust / M&A)
    "Banco Central",                         # BACEN
    "Comissão de Valores Mobiliários",       # CVM
    "Conselho Monetário Nacional",           # CMN
    "Conselho Nacional de Seguros",          # CNSP
    "Previdência Complementar",              # PREVIC
)


def fetch_dou(
    terms: Iterable[str],
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    sections: Iterable[str] = DEFAULT_SECTIONS,
    exact_date: str = "mes",
    organs: Iterable[str] = RELEVANT_ORGANS,
    *,
    today: dt.date | None = None,
    fetcher: Callable[[str, str, str], str] | None = None,
    pause_sec: float = 0.3,
    max_terms: int = 25,
) -> list[dict[str, Any]]:
    """Return recent DOU acts mentioning any of ``terms`` (quoted-phrase search)."""
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=lookback_days)
    fetch = fetcher or _fetch_query
    organ_filters = [o.lower() for o in (organs or [])]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    uniq_terms = [t for t in dict.fromkeys(str(t).strip() for t in terms) if t][:max_terms]
    for term in uniq_terms:
        for section in sections:
            for rec in _parse(fetch(term, section, exact_date), term):
                date = _parse_date(rec.get("date"))
                if not date or date < cutoff:
                    continue
                if organ_filters:
                    org = (rec.get("organ") or "").lower()
                    if not any(f in org for f in organ_filters):
                        continue
                if rec["id"] in seen:
                    continue
                seen.add(rec["id"])
                out.append(rec)
        if pause_sec:
            time.sleep(pause_sec)
    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    return out


def _fetch_query(term: str, section: str, exact_date: str) -> str:
    try:
        resp = requests.get(
            SEARCH_URL,
            params={"q": f'"{term}"', "s": section, "exactDate": exact_date, "sortType": "0"},
            timeout=30,
            headers={"User-Agent": "Onca-CI/1.0 (competitive-intelligence)"},
        )
        return resp.text if resp.status_code == 200 else ""
    except Exception as exc:  # pragma: no cover - upstream best-effort
        print(f"Warning: DOU query failed for {term}: {exc}")
        return ""


def _parse(html: str, term: str) -> list[dict[str, Any]]:
    m = _PARAMS_RE.search(html or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:  # pragma: no cover - scrape fragility
        return []
    out: list[dict[str, Any]] = []
    for it in data.get("jsonArray") or []:
        slug = (it.get("urlTitle") or "").strip()
        if not slug:
            continue
        title = (it.get("title") or "").strip()
        content = (it.get("content") or "").strip()
        out.append(
            {
                "id": f"dou:{slug}",
                "source": "DOU",
                "kind": "regulatory",
                "doc_type": (it.get("artType") or "Ato").strip(),
                "organ": (it.get("hierarchyStr") or "").strip() or None,
                "title": title,
                "subject": title,
                "text": content[:2000],
                "company": term,  # the matched competitor drives entity resolution
                "name": term,
                "date": _iso(it.get("pubDate")),
                "section": it.get("pubName"),
                "url": ACT_URL.format(slug=slug),
            }
        )
    return out


def _iso(value: Any) -> str:
    """DD/MM/YYYY -> YYYY-MM-DD (DOU pubDate); passthrough if already ISO."""
    s = (str(value) if value is not None else "").strip()
    if "/" in s:
        p = s.split("/")
        if len(p) == 3 and len(p[2]) == 4:
            return f"{p[2]}-{p[1].zfill(2)}-{p[0].zfill(2)}"
    return s[:10]


def _parse_date(value: Any) -> dt.date | None:
    s = (str(value) if value is not None else "").strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return dt.date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def inspect(terms: list[str] | None = None, lookback_days: int = 30) -> None:
    facts = fetch_dou(terms or ["BANCO BTG PACTUAL", "BRADESCO"], lookback_days=lookback_days)
    print(f"fetch_dou: {len(facts)} acts")
    for r in facts[:15]:
        print(f"  [{r['date']}] {r['section']} {(r['organ'] or '')[:30]:30} {r['title'][:50]}")


if __name__ == "__main__":
    import sys

    terms = sys.argv[1:] or None
    inspect(terms)
