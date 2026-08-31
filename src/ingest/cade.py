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
SECTIONS = ("do1", "do3")               # do1 normative, do3 = editais/despachos
_AC_RE = re.compile(r"Ato\s+de\s+Concentra[çc][ãa]o\s*n[ºo°]?\s*([\d.\-/]+)", re.I)
_PARTIES_RE = re.compile(
    r"Requerent[ea]s?\s*:?\s*(.+?)(?:\s*(?:Advogad|Procedimento|Natureza|Relator|"
    r"Aprova|Decido|Nos termos|Conselheir|$))",
    re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")


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


def fetch_atos(
    lookback_days: int = 45,
    *,
    fetcher: Callable[..., list[dict[str, Any]]] | None = None,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Recent CADE merger-review acts from the DOU, with AC number + parties extracted."""
    if fetcher is None:
        from src.ingest import dou
        fetcher = lambda: dou.fetch_dou(  # noqa: E731
            list(QUERIES), lookback_days=lookback_days, sections=SECTIONS,
            organs=ORGANS, today=today,
        )
    try:
        raw = fetcher() or []
    except Exception as exc:  # pragma: no cover - upstream best-effort
        print(f"Warning: CADE DOU fetch failed: {exc}")
        return []
    out: list[dict[str, Any]] = []
    for act in raw:
        blob = _clean(f"{act.get('title') or ''} {act.get('text') or act.get('subject') or ''}")
        if "concentra" not in blob.lower():           # keep only genuine merger acts
            continue
        slug = str(act.get("id") or "").replace("dou:", "")
        ac = _ac_number(blob)
        out.append({
            "id": f"cade:{slug}" if slug else f"cade:{ac or _clean(act.get('title'))[:40]}",
            "source": "CADE",
            "kind": "antitrust",
            "doc_type": act.get("doc_type") or "Ato",
            "ac_number": ac,
            "parties": _parties(blob),
            "title": _clean(act.get("title")),
            "text": blob[:2000],
            "organ": act.get("organ"),
            "date": act.get("date"),
            "url": act.get("url"),
        })
    return out


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
