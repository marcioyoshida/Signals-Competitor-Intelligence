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
    # #188: laws (Leis, Leis Complementares) and the Congress's Atos Declaratórios on MPs are
    # published under the LEGISLATIVE organs — LC 237 (resseguro) was invisible before.
    "Atos do Poder Legislativo",
    "Atos do Congresso Nacional",
    # #189: health insurers' regulator (Resoluções Normativas); topic hits scoped to DO1 rules.
    "Agência Nacional de Saúde Suplementar",
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
    "Atos do Poder Legislativo",   # #188
    "Atos do Congresso Nacional",  # #188
)
# #175: the betting regulator's DO1 acts (normative Portarias SPA/MF) are fetched in full too;
# its DO2 (personnel) and DO3 (editais de citação) keep the snippet.
FULL_TEXT_DO1_ORGANS = ("Secretaria de Prêmios e Apostas",)
# #189: an act kept by a doc-type-scoped issuer ("organ@DO1:Resolução") is a sector RULE and is
# fetched in full as well (flagged ``normative`` in fetch_dou).
FULL_TEXT_MAX_PER_RUN = 30
_PRIMARY_ORGANS = ("Atos do Poder Executivo", "Atos do Poder Legislativo", "Atos do Congresso Nacional")
# #191: the in.gov.br search returns 20 results unless ``delta`` asks for more; 75 is the
# largest value it honours (100/200/500 silently fall back to 20 — measured 2026-09-27), and
# it has no working page/offset parameter. A full page whose oldest act is still inside the
# lookback is walked back with a custom date window (exactDate=personalizado, publishTo = the
# oldest date seen), up to MAX_PAGES_PER_QUERY pages; a query still full after that is
# reported as SATURATED (LAST_STATS → source_health), never silently truncated.
PAGE_SIZE = 75
MAX_PAGES_PER_QUERY = 4
MAX_EXTRA_PAGES_PER_RUN = 40
#: per-run telemetry of the last fetch_dou call: queries, pages, saturated terms
LAST_STATS: dict[str, Any] = {}
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
    known_instruments: dict[str, list[str]] | None = None,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES_PER_QUERY,
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
    industries — the implementing acts of a ban need not repeat the sector's vocabulary.

    ``known_instruments`` ({"mp 1.394": ["betting"]}, #188 — ``registry.watched_acts``) are
    critical instruments from OUTSIDE this batch's window: acts citing them inherit industries.

    Paging (#191): each query asks for ``page_size`` results; a full page still inside the
    lookback is walked back by date window up to ``max_pages`` pages. ``LAST_STATS`` holds the
    run's query/page counts and the terms still saturated at the page limit."""
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
    stats: dict[str, Any] = {"queries": 0, "pages": 0, "saturated": [], "paged": [], "fuzzy": []}
    LAST_STATS.clear()
    LAST_STATS.update(stats)

    def _pages(term: str, section: str, literal: bool = False) -> tuple[list[dict[str, Any]], bool]:
        """Every result for (term, section) back to ``cutoff``, walking full pages back by date
        window (#191). Returns (records, got_any_response). ``literal`` (entity terms, #197):
        a full page that is mostly stem matches is not walked back — it is noise, not volume."""
        stats["queries"] += 1
        page = fetch(term, section, _window(exact_date, None, page_size))
        stats["pages"] += 1
        recs = _parse(page, term)
        got = bool(page)
        seen = {r["id"] for r in recs}
        n = 1
        while len(recs) and _full(page, recs, page_size):
            oldest = min((d for d in (_parse_date(r.get("date")) for r in recs[-page_size:]) if d),
                         default=None)
            if oldest is None or oldest <= cutoff:
                break
            if literal:
                rate = sum(1 for r in recs if literal_hit(r, term)) / len(recs)
                if rate < MIN_LITERAL_RATE:
                    stats["fuzzy"].append({"term": term, "section": section,
                                           "literal_rate": round(rate, 2)})
                    print(f"Info: DOU query {term!r} {section} is fuzzy "
                          f"({rate:.0%} literal hits on a full page); not paging")
                    break
            if n >= max_pages or stats["pages"] - stats["queries"] >= MAX_EXTRA_PAGES_PER_RUN:
                stats["saturated"].append({"term": term, "section": section,
                                           "oldest": oldest.isoformat(), "pages": n})
                print(f"Warning: DOU query saturated: {term!r} {section} — {n} full page(s), "
                      f"oldest {oldest} > cutoff {cutoff}; older acts not fetched")
                break
            page = fetch(term, section, _window(exact_date, (cutoff, oldest), page_size))
            stats["pages"] += 1
            n += 1
            more = [r for r in _parse(page, term) if r["id"] not in seen]
            if not more:
                # a single DAY holds more than a page: step past it rather than loop
                page = fetch(term, section, _window(exact_date, (cutoff, oldest - dt.timedelta(days=1)),
                                                    page_size))
                stats["pages"] += 1
                more = [r for r in _parse(page, term) if r["id"] not in seen]
                if not more:
                    break
                stats["saturated"].append({"term": term, "section": section, "oldest": oldest.isoformat(),
                                           "pages": n, "day_overflow": True})
            seen |= {r["id"] for r in more}
            recs += more
        if n > 1:
            stats["paged"].append({"term": term, "section": section, "pages": n})
        return recs, got

    def _run(plan_: list[tuple[str, bool]]) -> None:
        for term, is_topic in plan_:
            got = False
            for section in sections:
                page_recs, got_ = _pages(term, section, literal=not is_topic)
                got = got or got_
                for rec in page_recs:
                    date = _parse_date(rec.get("date"))
                    if not date or date < cutoff:
                        continue
                    if organ_filters:
                        org = (rec.get("organ") or "").lower()
                        if not any(f in org for f in organ_filters):
                            continue
                    if is_topic and not _organ_in_scope(rec.get("organ"), scopes.get(term), rec):
                        continue
                    # #197: the search stems ("CREDITAS" → "créditos"), so an entity hit must
                    # carry the name itself — else MP 1.393 was bound to Creditas
                    if not is_topic and not literal_hit(rec, term):
                        continue
                    if is_topic:
                        rec.update(company=None, name=None, topic_term=term, industries=list(topics[term]))
                        if _doc_scoped_match(rec, scopes.get(term)):
                            rec["normative"] = True
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
                    if rec.get("normative"):
                        prev["normative"] = True
            if pause_sec and got:  # politeness pause after a real response only
                time.sleep(pause_sec)

    def _finish() -> list[dict[str, Any]]:
        out_ = sorted(by_id.values(), key=lambda r: r.get("date") or "", reverse=True)
        if full_text:
            _attach_full_text(out_, act_fetcher or _fetch_act)
        if classify:
            from src.ingest import federal_acts

            federal_acts.annotate(out_, known_instruments=known_instruments)
        LAST_STATS.update(stats)
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


# #197: in.gov.br stems the quoted query ("CREDITAS" matches "crédito(s)": 75/75 hits on a page,
# 2 kept, both mis-bound to the company). Entity terms need the literal name.
MIN_LITERAL_RATE = 0.2


def _fold_ascii(s: Any) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", _html.unescape(str(s or "")))
    return re.sub(r"\s+", " ", "".join(c for c in t if not unicodedata.combining(c))).lower()


def literal_hit(rec: dict[str, Any], term: str) -> bool:
    """True when ``term`` occurs as whole words in the record's title/snippet (accent-folded)."""
    t = _fold_ascii(term).strip()
    if not t:
        return False
    blob = _fold_ascii(re.sub(r"<[^>]+>", " ",   # the snippet's highlight <span>s
                              " ".join(str(rec.get(k) or "") for k in ("title", "subject", "text"))))
    return re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", blob) is not None


def _norm(s: Any) -> str:
    # the DOU hierarchy has stray double spaces ("Diretoria de  Normas"); case-folded
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _scope_hit(organ: str | None, scope: str, rec: dict[str, Any] | None) -> tuple[bool, bool]:
    """(matches, doc-type-scoped). ``scope`` = "organ", "organ$" (exact) or
    "organ@DO1:Type,Type" (#189: only DO1-edition acts whose doc type starts with a Type)."""
    sc, _, doc_rule = str(scope).partition("@")
    sc = _norm(sc)
    org = _norm(organ)
    if sc.endswith("$"):
        ok = org == sc[:-1].strip()
    else:
        ok = bool(sc) and sc in org
    if not ok or not doc_rule:
        return ok, False
    if rec is None:
        return False, True
    sect_rule, _, types = doc_rule.partition(":")
    if sect_rule and not _norm(rec.get("section")).startswith(_norm(sect_rule)):
        return False, True
    dtype = _norm(rec.get("doc_type"))
    if types and not any(dtype.startswith(_norm(t)) for t in types.split(",") if t.strip()):
        return False, True
    return True, True


def _organ_in_scope(organ: str | None, scopes: Iterable[str] | None,
                    rec: dict[str, Any] | None = None) -> bool:
    """True when ``organ`` matches one of ``scopes`` (substring; ``X$`` = exactly X;
    ``X@DO1:Resolução`` = X's DO1 Resoluções only, which needs ``rec``). No scopes = no restriction."""
    if not scopes:
        return True
    return any(_scope_hit(organ, sc, rec)[0] for sc in scopes)


def _doc_scoped_match(rec: dict[str, Any], scopes: Iterable[str] | None) -> bool:
    """True when ``rec`` was kept by a doc-type-scoped (normative-rule) issuer entry."""
    return any(hit and scoped for hit, scoped in
               (_scope_hit(rec.get("organ"), sc, rec) for sc in (scopes or [])))


def _window(exact_date: str, window: tuple[dt.date, dt.date] | None, page_size: int) -> str:
    """The fetcher's date argument: ``exact_date`` ("mes"), or for a walked-back page
    ``"personalizado:DD-MM-YYYY:DD-MM-YYYY"``; ``|delta=N`` carries the page size. Kept as ONE
    string so a 3-argument fetcher (tests) still works."""
    base = exact_date if window is None else (
        f"personalizado:{window[0].strftime('%d-%m-%Y')}:{window[1].strftime('%d-%m-%Y')}")
    return f"{base}|delta={int(page_size)}" if page_size and page_size != 20 else base


def _full(page: str, recs: list[dict[str, Any]], page_size: int) -> bool:
    """True when the page came back at its size limit (more results exist beyond it)."""
    m = _PARAMS_RE.search(page or "")
    if not m:
        return False
    try:
        n = len(json.loads(m.group(1)).get("jsonArray") or [])
    except Exception:  # pragma: no cover
        return False
    return n >= page_size


def _attach_full_text(recs: list[dict[str, Any]], fetch_act: Callable[[str], str]) -> None:
    """Replace the search snippet with the act's full text for sector-wide normative acts
    (``FULL_TEXT_ORGANS``), capped per run. Best-effort: on failure the snippet stays."""
    def _rank(r: dict[str, Any]) -> int | None:
        org = r.get("organ") or ""
        do1 = str(r.get("section") or "").upper().startswith("DO1")
        if _organ_in_scope(org, _PRIMARY_ORGANS):
            return 0          # MPs, Decretos, Leis: the acts everything else cites
        if r.get("normative") or _organ_in_scope(org, FULL_TEXT_ORGANS):
            return 1          # sector rules (#189), CMN, Portarias MF, Presidência despachos
        if do1 and _organ_in_scope(org, FULL_TEXT_DO1_ORGANS):
            return 2          # SPA DO1 — many per-operator acts after a ban: must not starve 0/1
        return None

    # #188: the cap is spent by PRIORITY, newest first within a tier — before this, 41 SPA acts
    # after the betting ban used the whole budget and LC 237 kept its snippet (severity "low").
    ranked = sorted(((k, i) for i, r in enumerate(recs) if (k := _rank(r)) is not None
                     and not r.get("full_text")), key=lambda x: x[0])
    n = 0
    for _, i in ranked:
        r = recs[i]
        if n >= FULL_TEXT_MAX_PER_RUN:
            break
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
    base, _, delta = exact_date.partition("|delta=")
    params: dict[str, str] = {"q": f'"{term}"', "s": section, "exactDate": base, "sortType": "0"}
    if base.startswith("personalizado:"):
        _, frm, to = base.split(":")
        params.update(exactDate="personalizado", publishFrom=frm, publishTo=to)
    if delta:
        params["delta"] = delta
    try:
        resp = requests.get(
            SEARCH_URL,
            params=params,
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
