"""Ingest CADE antitrust merger reviews — the M&A signal (issue #61).

CADE (Conselho Administrativo de Defesa Econômica) reviews **Atos de Concentração**
(mergers/acquisitions/JVs above the revenue thresholds of Lei 12.529/2011). Approvals,
challenges and tribunal judgments are the highest-value strategic events and are
**sector-agnostic** — who is buying/merging with whom, in any industry.

CADE has no reachable open-data endpoint (dadosabertos.cade.gov.br is down; its data on
dados.gov.br is 401). But CADE publishes every ato in the **DOU** under the *Defesa
Econômica* organ, so we reuse the DOU scrape (`dou.fetch_dou`) with a merger-review query
and organ filter, then extract the **Ato de Concentração** process number and the
**Requerentes** (parties) from the act text.

This differs from the generic `dou` lens (which only keeps acts *mentioning a tracked
competitor*): here we pull ALL merger reviews and resolve the parties, so a deal is
captured even when a party is a not-yet-tracked target — and it is tagged as a dedicated
`antitrust`/M&A signal rather than a generic regulatory act.

Resolution is by party NAME (via the injected ``resolver``) — appropriate here because a
merger act names its parties as the explicit subjects (unlike the CEIS/CNEP sanctions
source, where fuzzy name matching would mis-attribute). Best-effort: degrades to ``[]``.
"""
from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any, Callable, Iterable

# DOU phrase queries that surface merger reviews (quoted-phrase search in dou.fetch_dou).
QUERIES = ("Ato de Concentração",)
ORGANS = ("Defesa Econômica",)          # CADE's DOU hierarchy tag
# #192/R6: "todos" is the only section value that includes every DOU edition (do1/do3 plus
# the EXTRA editions) — see dou.py's DEFAULT_SECTIONS note. A CADE edital or despacho can
# legitimately land in an extra edition, and "do1"/"do3" alone silently drop it.
SECTIONS = ("todos",)
_AC_RE = re.compile(r"Ato\s+de\s+Concentra[çc][ãa]o\s*n[ºo°]?\s*([\d.\-/]+)", re.I)
# Editais say "Partes:"; despachos/pautas say "Requerentes:" — both name the merging parties.
_PARTIES_RE = re.compile(
    r"(?:Requerent[ea]s?|Partes)\s*:?\s*(.+?)(?:\s*(?:Advogad|Procedimento|Natureza|Relator|"
    r"Aprova|Decido|Nos termos|Conselheir|Ato\s+de\s+Concentra|$))",
    re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
# Polite cap on live act-page fetches per run (one edital routinely bundles several ACs, so a
# lookback batch is small — this mirrors dou.py's FULL_TEXT_MAX_PER_RUN order of magnitude).
FULL_TEXT_MAX_PER_RUN = 25


def _clean(text: Any) -> str:
    return html.unescape(_TAG_RE.sub("", str(text or ""))).strip()


def _ac_number(text: str) -> str | None:
    m = _AC_RE.search(text)
    return m.group(1).strip(" .") if m else None


def _parties(text: str) -> str | None:
    m = _PARTIES_RE.search(text)
    if not m:
        return None
    val = re.sub(r"\s+", " ", m.group(1)).strip(" .;,")
    return val[:300] or None


def _iter_acs(text: str) -> list[tuple[str, str | None]]:
    """Yield ``(ac_number, parties)`` for EVERY ``Ato de Concentração nº …`` block in
    ``text`` (#192/R6). A single edital or despacho routinely bundles several unrelated
    ACs (each with its own "Requerentes:"/"Partes:" line) — the old single-match regex
    only ever surfaced the first one, and search snippets cut most of them off anyway."""
    matches = list(_AC_RE.finditer(text or ""))
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for i, m in enumerate(matches):
        ac = m.group(1).strip(" .")
        if not ac or ac in seen:            # de-dupe repeats within the same act text
            continue
        seen.add(ac)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((ac, _parties(text[m.end():end])))
    return out


def fetch_atos(
    lookback_days: int = 45,
    *,
    fetcher: Callable[..., list[dict[str, Any]]] | None = None,
    act_fetcher: Callable[[str], str] | None = None,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Recent CADE merger-review acts from the DOU, one record per Ato de Concentração
    (an act can hold several — see ``_iter_acs``), with AC number + parties extracted from
    the act's FULL TEXT, not the search snippet (#192/R6: the snippet is what previously
    made ``parties`` come back empty on every edital and garbled on despachos)."""
    from src.ingest import dou

    if fetcher is None:
        fetcher = lambda: dou.fetch_dou(  # noqa: E731
            list(QUERIES), lookback_days=lookback_days, sections=SECTIONS,
            organs=ORGANS, today=today, full_text=False, classify=False,
            follow_citations=False,
        )
        if act_fetcher is None:
            act_fetcher = dou._fetch_act
    fetch_act = act_fetcher or (lambda _url: "")   # tests: no live call unless given one
    try:
        raw = fetcher() or []
    except Exception as exc:  # pragma: no cover - upstream best-effort
        print(f"Warning: CADE DOU fetch failed: {exc}")
        return []
    out: list[dict[str, Any]] = []
    n_full_text = 0
    for act in raw:
        slug = str(act.get("id") or "").replace("dou:", "")
        blob = _clean(f"{act.get('title') or ''} {act.get('text') or act.get('subject') or ''}")
        full_text = bool(act.get("full_text"))
        if not full_text and act.get("url") and n_full_text < FULL_TEXT_MAX_PER_RUN:
            n_full_text += 1
            body = dou.extract_act_text(fetch_act(act["url"]))
            if body:
                blob = _clean(f"{act.get('title') or ''} {body}")
                full_text = True
        if "concentra" not in blob.lower():           # keep only genuine merger acts
            continue
        acs = _iter_acs(blob)
        if not acs:                                    # matched "concentra" but no AC number
            acs = [(None, _parties(blob))]
        for ac, parties in acs:
            out.append({
                "id": f"cade:{slug}:{ac}" if slug and ac
                      else (f"cade:{slug}" if slug else f"cade:{ac or _clean(act.get('title'))[:40]}"),
                "source": "CADE",
                "kind": "antitrust",
                "doc_type": act.get("doc_type") or "Ato",
                "ac_number": ac,
                "parties": parties,
                "title": _clean(act.get("title")),
                "text": blob[:2000],
                "organ": act.get("organ"),
                "date": act.get("date"),
                "url": act.get("url"),
                "full_text": full_text,
            })
    _record_yield(out, lookback_days=lookback_days)
    return out


def _record_yield(atos: list[dict[str, Any]], *, lookback_days: int) -> None:
    """#192/R6: a distinct-ACs-per-week yield metric into source_health, so a "live/ok"
    status can't hide a lens that runs fine and fetches docs but extracts nothing usable
    (Fontes tab showed "no ar / ok / 30 narrativas" while the digest held ONE distinct AC
    in 30 days). Best-effort: telemetry must never affect ingestion."""
    try:
        from src.ingest import source_health

        distinct = len({a["ac_number"] for a in atos if a.get("ac_number")})
        weeks = max(lookback_days, 1) / 7.0
        source_health.record(
            "cade", ok=True, docs=len(atos),
            metrics={"distinct_acs": distinct, "acs_per_week": round(distinct / weeks, 2)},
        )
    except Exception:  # pragma: no cover - telemetry must never affect ingestion
        pass


def map_to_entities(
    atos: Iterable[dict[str, Any]],
    *,
    resolver: Callable[[dict[str, Any]], list[str]],
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Keep merger acts whose parties resolve to ≥1 tracked entity, as signal records.

    A merger legitimately involves several subjects, so ``_entities`` carries EVERY
    tracked party — the card then surfaces for each. Acts with no tracked party are
    dropped here (they are M&A-discovery candidates, out of scope for the feed).
    """
    today = today or dt.date.today()
    out: list[dict[str, Any]] = []
    for a in atos or []:
        try:
            ents = resolver({
                "source": "DOU", "title": a.get("title"),
                "text": a.get("text"), "institution": a.get("parties"),
            }) or []
        except Exception:  # pragma: no cover - resolver best-effort
            ents = []
        if not ents:
            continue
        out.append({
            **a,
            "id": f"antitrust:{a['id']}",
            "entity": ents[0],
            "_entities": list(dict.fromkeys(ents)),
            "mapped_at": today.isoformat(),
        })
    return out


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_entity: dict[str, int] = {}
    for r in records:
        for e in r.get("_entities") or [r.get("entity")]:
            if e:
                by_entity[e] = by_entity.get(e, 0) + 1
    return {
        "kind": "cade_merger_reviews",
        "source": "CADE",
        "total": len(records),
        "entities": len(by_entity),
        "acs": sorted({r["ac_number"] for r in records if r.get("ac_number")}),
        "top": sorted(({"entity": e, "count": c} for e, c in by_entity.items()),
                      key=lambda x: -x["count"])[:5],
    }


def run(today: dt.date | None = None) -> dict[str, Any]:
    """Fetch → map. Standalone entrypoint (no durable store — acts are event signals)."""
    from src.synth.entities import resolve_entities
    atos = fetch_atos(today=today)
    recs = map_to_entities(atos, resolver=resolve_entities, today=today)
    return {"acts": len(atos), "mapped": len(recs), **summarize(recs)}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False))
