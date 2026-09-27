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
import html as _html
import json
import re
import time
from typing import Any, Callable, Iterable

import requests

SEARCH_URL = "https://www.in.gov.br/consulta/-/buscar/dou"
ACT_URL = "https://www.in.gov.br/web/dou/-/{slug}"
DEFAULT_LOOKBACK_DAYS = 30
# #173/#174: "todos" is the ONLY section value whose results include the EXTRA editions
# (DO1_EXTRA_A/B...), where same-day Medidas Provisórias, decrees and Portarias MF run —
# "do1" and "do1e" both miss them (verified live 2026-09-27: MP 1.394, the online-betting
# ban, was invisible to "do1"). DO2/DO3 noise is controlled by the organ filter below.
DEFAULT_SECTIONS = ("todos",)
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
    # CoAF / UIF (issue #24) — AML/PLD enforcement, sanctions and normative acts.
    # CoAF has no clean public data API (dados.gov.br empty, UIF endpoint 404); its
    # public acts are published here in the DOU, so one organ filter covers it.
    "Controle de Atividades Financeiras",    # COAF
    "Unidade de Inteligência Financeira",    # UIF (CoAF's current name)
    # #173/#174: the issuers of sector-wide measures. Without these, MP 1.394 (online-betting
    # ban, "Atos do Poder Executivo") and every act of the betting regulator SPA (under
    # Ministério da Fazenda) were filtered out — the betting DOU terms were dead config.
    "Presidência da República",              # despachos, vetos
    "Atos do Poder Executivo",               # Medidas Provisórias, Decretos
    "Secretaria de Prêmios e Apostas",       # SPA (betting regulator), Min. Fazenda
    "Ministério da Fazenda/Gabinete do Ministro",  # Portarias MF (scoped: not all of Fazenda)
)

# Organs whose acts are fetched IN FULL (not just the search snippet): sector-wide
# normative acts are rare (a few a day) and are exactly what the CRO and /api/ask must
# be able to quote. The rest keep the snippet (DO3 editais, routine despachos).
FULL_TEXT_ORGANS = (
    "Atos do Poder Executivo",
    # #175: the Presidência's own despachos/mensagens (exact organ — not ABIN/Casa Civil
    # sub-organs): short, and the snippet cuts the "Encaminhamento … da Medida Provisória nº"
    # line that ties them to the act they forward.
    "Presidência da República$",
    "Ministério da Fazenda/Gabinete do Ministro",
    "Conselho Monetário Nacional",
)
# #175: the betting regulator's DO1 acts (normative Portarias SPA/MF) are fetched in full too;
# its DO2 (personnel) and DO3 (editais de citação) keep the snippet.
FULL_TEXT_DO1_ORGANS = ("Secretaria de Prêmios e Apostas",)
FULL_TEXT_MAX_PER_RUN = 12
_TEXTO_RE = re.compile(r'<div class="texto-dou">(.*?)</div>\s*</div>', re.S)


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
    topic_terms: dict[str, list[str]] | None = None,
    max_topic_terms: int = 15,
    full_text: bool = True,
    act_fetcher: Callable[[str], str] | None = None,
    topic_organs: dict[str, list[str]] | None = None,
    classify: bool = True,
    follow_citations: bool = True,
    max_citation_follow: int = 4,
    citation_organs: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Return recent DOU acts mentioning any of ``terms`` (competitor names, quoted-phrase
    search) or any ``topic_terms`` ({phrase: [industry slugs]}).

    Topic terms (#174) have their OWN budget and run FIRST, so a growing competitor list can
    never push them past ``max_terms``. A topic-matched act carries ``industries`` and no
    entity (``company``/``name`` None) unless a competitor term also matched it — then it
    keeps the competitor binding AND gains the industries.

    ``topic_organs`` ({phrase: [organ scopes]}, #175) narrows a topic phrase's hits to the
    sector-wide issuers (``registry.NORMATIVE_ISSUERS``); a scope ending in ``$`` must equal
    the organ. ``classify`` (#175) runs ``federal_acts.annotate`` so every act leaving here
    carries ``severity`` (and ``industries`` when a covered industry applies).

    ``follow_citations`` (#175): for each CRITICAL MP/Lei/Decreto found (≤ ``max_citation_follow``),
    one more search for the acts citing it ("Medida Provisória nº 1.394"), scoped to
    ``citation_organs`` (default ``registry.NORMATIVE_ISSUERS``); those acts inherit its
    industries — the implementing acts of a ban need not repeat the sector's vocabulary."""
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=lookback_days)
    fetch = fetcher or _fetch_query
    organ_filters = [o.lower() for o in (organs or [])]
    by_id: dict[str, dict[str, Any]] = {}
    topics = {str(k).strip(): list(v or []) for k, v in (topic_terms or {}).items() if str(k).strip()}
    scopes = dict(topic_organs or {})
    plan = [(t, True) for t in list(topics)[:max_topic_terms]]
    plan += [(t, False) for t in [t for t in dict.fromkeys(str(t).strip() for t in terms) if t
                                  and t not in topics][:max_terms]]

    def _run(plan_: list[tuple[str, bool]]) -> None:
        for term, is_topic in plan_:
            got = False
            for section in sections:
                page = fetch(term, section, exact_date)
                got = got or bool(page)
                for rec in _parse(page, term):
                    date = _parse_date(rec.get("date"))
                    if not date or date < cutoff:
                        continue
                    if organ_filters:
                        org = (rec.get("organ") or "").lower()
                        if not any(f in org for f in organ_filters):
                            continue
                    if is_topic and not _organ_in_scope(rec.get("organ"), scopes.get(term)):
                        continue
                    if is_topic:
                        rec.update(company=None, name=None, topic_term=term, industries=list(topics[term]))
                    prev = by_id.get(rec["id"])
                    if prev is None:
                        by_id[rec["id"]] = rec
                        continue
                    # merge: keep an entity binding if any term gave one; union industries
                    if not prev.get("company") and rec.get("company"):
                        prev.update(company=rec["company"], name=rec["name"])
                    if rec.get("industries"):
                        prev["industries"] = sorted(set(prev.get("industries") or []) | set(rec["industries"]))
                        prev.setdefault("topic_term", rec.get("topic_term"))
            if pause_sec and got:  # politeness pause after a real response only
                time.sleep(pause_sec)

    def _finish() -> list[dict[str, Any]]:
        out_ = sorted(by_id.values(), key=lambda r: r.get("date") or "", reverse=True)
        if full_text:
            _attach_full_text(out_, act_fetcher or _fetch_act)
        if classify:
            from src.ingest import federal_acts

            federal_acts.annotate(out_)
        return out_

    _run(plan)
    out = _finish()
    if classify and follow_citations:
        # #175: a critical MP/Lei/Decreto in this batch (MP 1.394) → search the DOU for the acts
        # that CITE it (the implementing Portaria, the SPA rules, the despacho), scoped to the
        # normative issuers. They inherit its industries in federal_acts.annotate (citation rule).
        from src.ingest import federal_acts, registry

        anchors = federal_acts.critical_anchors(out)
        follow = [(federal_acts.citation_phrase(ref), inds) for ref, inds in anchors.items()]
        follow = [(p, i) for p, i in follow if p not in topics][:max_citation_follow]
        if follow:
            for p, inds in follow:
                topics[p] = list(inds)
                scopes[p] = list(citation_organs if citation_organs is not None
                                 else registry.NORMATIVE_ISSUERS)
            _run([(p, True) for p, _ in follow])
            out = _finish()
    return out


def _organ_in_scope(organ: str | None, scopes: Iterable[str] | None) -> bool:
    """True when ``organ`` matches one of ``scopes`` (substring; ``X$`` = exactly X). No scopes =
    no restriction."""
    if not scopes:
        return True
    org = (organ or "").strip().lower()
    for sc in scopes:
        sc = str(sc).strip().lower()
        if sc.endswith("$"):
            if org == sc[:-1]:
                return True
        elif sc and sc in org:
            return True
    return False


def _attach_full_text(recs: list[dict[str, Any]], fetch_act: Callable[[str], str]) -> None:
    """Replace the search snippet with the act's full text for sector-wide normative acts
    (``FULL_TEXT_ORGANS``), capped per run. Best-effort: on failure the snippet stays."""
    n = 0
    for r in recs:
        if n >= FULL_TEXT_MAX_PER_RUN:
            break
        if r.get("full_text"):
            continue
        org = r.get("organ") or ""
        do1 = str(r.get("section") or "").upper().startswith("DO1")
        if not (_organ_in_scope(org, FULL_TEXT_ORGANS)
                or (do1 and _organ_in_scope(org, FULL_TEXT_DO1_ORGANS))):
            continue
        body = extract_act_text(fetch_act(r["url"]))
        n += 1
        if body:
            r["text"] = body[:20000]
            r["full_text"] = True


def extract_act_text(page: str) -> str:
    """The act body from an in.gov.br act page (``div.texto-dou``), tags stripped."""
    m = _TEXTO_RE.search(page or "")
    if not m:
        return ""
    txt = re.sub(r"<[^>]+>", " ", m.group(1))
    return _html.unescape(re.sub(r"\s+", " ", txt)).strip()


def _fetch_act(url: str) -> str:
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Onca-CI/1.0 (competitive-intelligence)"})
        return resp.text if resp.status_code == 200 else ""
    except Exception as exc:  # pragma: no cover - upstream best-effort
        print(f"Warning: DOU act fetch failed for {url}: {exc}")
        return ""


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
        # the title carries <span class='highlight'> markup when the query hits it (e.g. a
        # "PORTARIA MF Nº 2.946" search): strip it like the snippet (#175)
        title = _html.unescape(re.sub(r"<[^>]+>", "", it.get("title") or "")).strip()
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
                # the search snippet carries <span class='highlight'> markup: strip it
                "text": _html.unescape(re.sub(r"<[^>]+>", "", content))[:2000],
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
