"""ADR 021 §D/§G — the per-officer executive block (`feed.executive`).

Read-track slice: a DERIVED, per-industry-aware **CSO Executive Dashboard** block built
entirely from fields the feed already carries (feed cards, groups, distress, integrity,
regulatory lifecycle, kpis) — no new data, no fabrication. Every panel item references a real
card `id` so the v3 app can link to its grounding; the one composite score (the Strategic
Climate Index) is a transparent, documented formula surfaced as an *inference*, never a fact.

Industry scoping (§G) is a client-side filter: each panel item carries `industries`, and the
per-industry aggregates are precomputed here under `by_industry[slug]` (+ `__all__`). CRO/CCO/CPO
blocks are the next slice; `build_executive` already lists only the officers it actually builds.
"""
from __future__ import annotations

from typing import Any

ALL = "__all__"

# Card lenses/topics that read as a competitive "strategic move" (used for the M&A / moves panel).
_MOVE_TOPICS = {"concorrencia", "novos_entrantes"}
_MOVE_LENSES = {"entrants", "ofertas", "market"}
_MA_CUES = ("aquisi", "fusão", "fusao", "compra", "incorpora", "joint venture", "m&a", "cade")


def _cards(feed: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in (feed.get("feed") or []) if isinstance(c, dict)]


def _in_industry(card: dict[str, Any], slug: str | None) -> bool:
    """A card belongs to a sector iff its denormalized `industries` (or, for reg cards,
    `affected_industries`) includes the slug. slug None/ALL ⇒ every card."""
    if not slug or slug == ALL:
        return True
    inds = set(card.get("industries") or []) | set(card.get("affected_industries") or [])
    return slug in inds


def _threat(card: dict[str, Any]) -> float:
    """Threat on a 0–100 scale. Feed threat_score is 0–1 (e.g. 0.806); normalize so the
    climate index, thresholds and displayed averages are all in the same 0–100 space."""
    try:
        v = float(card.get("threat_score") or 0)
    except (TypeError, ValueError):
        return 0.0
    return v * 100 if v <= 1 else v


def _trusted_distress(feed: dict[str, Any]) -> list[dict[str, Any]]:
    """Distress entries safe to surface on an executive/board surface: CONFIRMED confidence
    only (ADR-013's CVM-fatos-grade seed). `reported`-confidence news distress is excluded —
    it carries the #33 counterparty mis-attribution risk (a bank named in a retailer's RJ news
    must never be shown as itself insolvent). Defamation guardrail, applied at the delivery edge."""
    return [d for d in (feed.get("distress") or []) if d.get("confidence") == "confirmed"]


def _recent_window(dates: list[str]) -> tuple[set[str], set[str]]:
    """(recent 7 dates, prior 7 dates) from the feed's sorted date list."""
    ds = sorted(str(d) for d in (dates or []))
    return set(ds[-7:]), set(ds[-14:-7])


def _climate_index(cards: list[dict[str, Any]], n_distress: int) -> int:
    """Strategic Climate Index (0–100, higher = calmer). Transparent composite — labelled an
    inference in the UI, never a fact: 100 minus the recent average competitor-threat (0–100),
    the alert ratio, and a distress penalty. Empty cohort ⇒ neutral 50."""
    if not cards:
        return 50
    avg_threat = sum(_threat(c) for c in cards) / len(cards)
    alert_ratio = sum(1 for c in cards if c.get("is_alert")) / len(cards)
    penalty = 0.6 * avg_threat + 0.3 * (alert_ratio * 100) + 0.1 * min(n_distress * 20, 100)
    return max(0, min(100, round(100 - penalty)))


def _aggregate(cards: list[dict[str, Any]], reg: list[dict[str, Any]],
               n_distress: int, n_moves: int) -> dict[str, Any]:
    n = len(cards)
    alerts = sum(1 for c in cards if c.get("is_alert"))
    avg = round(sum(_threat(c) for c in cards) / n, 1) if n else 0.0
    reg_threat = round(sum(_threat(c) for c in reg) / len(reg), 1) if reg else 0.0
    return {
        "climate": _climate_index(cards, n_distress),
        "n_cards": n, "n_alerts": alerts, "avg_threat": avg,
        "n_risks": alerts + n_distress, "reg_threat": reg_threat,
        "n_reg": len(reg), "distress": n_distress, "n_moves": n_moves,
    }


def _headline(card: dict[str, Any]) -> dict[str, Any]:
    """Minimal citable projection — the v3 app resolves the full card from feed.feed by id."""
    return {
        "id": card.get("id"),
        "entity_label": card.get("entity_label") or card.get("entity") or "—",
        "date": card.get("date"),
        "threat_score": card.get("threat_score"),
        "is_alert": bool(card.get("is_alert")),
        "industries": card.get("industries") or [],
        "title": (card.get("narrative") or "")[:140],
    }


def _is_move(card: dict[str, Any]) -> bool:
    if set(card.get("topics") or []) & _MOVE_TOPICS or set(card.get("lenses") or []) & _MOVE_LENSES:
        return True
    blob = (card.get("narrative") or "").lower()
    return any(cue in blob for cue in _MA_CUES)


def _momentum(cards: list[dict[str, Any]], dates: list[str],
              labels: dict[str, str]) -> list[dict[str, Any]]:
    """Per-entity competitor momentum = recent-window avg threat − prior-window avg threat.
    Positive ⇒ the competitor is gaining ground (rising threat)."""
    recent, prior = _recent_window(dates)
    acc: dict[str, dict[str, list[float]]] = {}
    inds: dict[str, set[str]] = {}
    for c in cards:
        e = c.get("entity")
        if not e:
            continue
        d = str(c.get("date") or "")
        bucket = "recent" if d in recent else ("prior" if d in prior else None)
        if bucket is None:
            continue
        acc.setdefault(e, {"recent": [], "prior": []})[bucket].append(_threat(c))
        inds.setdefault(e, set()).update(c.get("industries") or [])
    out: list[dict[str, Any]] = []
    for e, w in acc.items():
        if not w["recent"]:
            continue
        rec = sum(w["recent"]) / len(w["recent"])
        pri = sum(w["prior"]) / len(w["prior"]) if w["prior"] else rec
        out.append({
            "entity": e, "label": labels.get(e, e),
            "recent": round(rec, 1), "prior": round(pri, 1),
            "momentum": round(rec - pri, 1), "industries": sorted(inds.get(e, set())),
        })
    out.sort(key=lambda x: x["momentum"], reverse=True)
    return out


def _recommendations(cards: list[dict[str, Any]], reg: list[dict[str, Any]],
                     distress: list[dict[str, Any]], momentum: list[dict[str, Any]],
                     labels: dict[str, str]) -> list[dict[str, Any]]:
    """Honest, horizon-bucketed recommendations, each grounded in a real card/entity and mapped
    to an ADR-020 catalog action. Capture (aprovar/rejeitar + outcome) renders DISABLED until the
    ADR-021 decision backend lands — these are read-track suggestions, never auto-applied."""
    recs: list[dict[str, Any]] = []

    def add(horizon: str, text: str, action: str, *, entity: str | None = None,
            evidence_id: str | None = None, industries: list[str] | None = None):
        recs.append({"horizon": horizon, "text": text, "action": action, "entity": entity,
                     "evidence_id": evidence_id, "industries": industries or []})

    # Imediato — a fresh insolvency signal on the roster (escalate to Compliance).
    for dst in distress[:2]:
        e = dst.get("entity")
        add("imediato", f"Escalar risco de insolvência: {labels.get(e, e)} ({dst.get('label')})",
            "flag_entity", entity=e,
            evidence_id=(dst.get("evidence") or [None])[0], industries=[])
    # Imediato — the highest-threat alert card ON A REAL COMPETITOR (not a regulatory/placeholder
    # card) → open a strategic watch.
    named_alerts = sorted(
        (c for c in cards if c.get("is_alert") and c.get("entity") in labels
         and c.get("kind") not in ("regulatory_lifecycle", "regulatory_fusion")),
        key=_threat, reverse=True)
    if named_alerts:
        top = named_alerts[0]
        add("imediato", f"Abrir watch estratégico: {top.get('entity_label') or top.get('entity')}",
            "open_watch", entity=top.get("entity"), evidence_id=top.get("id"),
            industries=top.get("industries") or [])
    # 30 dias — a regulatory change with the widest blast-radius.
    reg_sorted = sorted(reg, key=lambda c: len(c.get("affected_industries") or []), reverse=True)
    if reg_sorted:
        r = reg_sorted[0]
        n_aff = len(r.get("affected_industries") or [])
        add("30d", f"Avaliar mudança regulatória — {r.get('domain') or 'regulação'} "
                   f"(afeta {n_aff} setor{'es' if n_aff != 1 else ''})",
            "open_watch", evidence_id=r.get("id"), industries=r.get("affected_industries") or [])
    # 90 dias — the competitor gaining the most momentum → curate a thesis.
    rising = [m for m in momentum if m["momentum"] > 0][:1]
    if rising:
        m = rising[0]
        add("90d", f"Formular tese sobre o avanço de {m['label']} (momentum +{m['momentum']})",
            "curate_belief", entity=m["entity"], industries=m["industries"])
    return recs


def build_cso(feed: dict[str, Any]) -> dict[str, Any]:
    """The CSO (Estrategista-chefe) Executive Dashboard block — §D panels, industry-scoped (§G)."""
    cards = _cards(feed)
    dates = list(feed.get("dates") or [])
    recent, _prior = _recent_window(dates)
    labels: dict[str, str] = {}
    for e in (feed.get("entities") or []):
        if e.get("entity"):
            labels[e["entity"]] = e.get("label") or e["entity"]
    for eid, a in (feed.get("entity_attrs") or {}).items():
        labels.setdefault(eid, (a or {}).get("label") or eid)

    reg_cards = [c for c in cards if c.get("kind") in ("regulatory_lifecycle", "regulatory_fusion")]
    distress = _trusted_distress(feed)  # confirmed-only; keeps #33 mis-attributions off the board

    # Panels (all-industry lists; the client filters by the selected sector).
    news = [c for c in cards if c.get("kind") != "regulatory_lifecycle"]
    headlines = sorted(news, key=lambda c: (str(c.get("date") or ""), _threat(c)), reverse=True)
    emerging = [c for c in news if str(c.get("date") or "") in recent and _threat(c) >= 60]
    risks = sorted((c for c in cards if c.get("is_alert")), key=_threat, reverse=True)
    opportunities = [c for c in cards
                     if (c.get("swot_hint") or {}).get("dimension") == "O"
                     or (c.get("swot_hint") or {}).get("sign") == "-"]  # competitor weakness = our opening
    moves = [c for c in news if _is_move(c)]
    momentum = _momentum(cards, dates, labels)

    # Per-industry aggregates (+ __all__) for the selector's headline tiles / climate index.
    sectors = [{"slug": o.get("slug"), "label": o.get("display_name") or o.get("label") or o.get("slug")}
               for o in (feed.get("industry_options") or []) if o.get("slug")]
    by_industry: dict[str, Any] = {}
    for slug in [ALL] + [s["slug"] for s in sectors]:
        sc = [c for c in cards if _in_industry(c, slug)]
        sreg = [c for c in reg_cards if _in_industry(c, slug)]
        sdist = [d for d in distress
                 if slug in (ALL, None) or slug in set(
                     (feed.get("entity_attrs") or {}).get(d.get("entity"), {}).get("industries") or [])]
        smoves = [c for c in moves if _in_industry(c, slug)]
        by_industry[slug] = _aggregate(sc, sreg, len(sdist), len(smoves))

    return {
        "sectors": sectors,
        "by_industry": by_industry,
        "panels": {
            "headlines": [_headline(c) for c in headlines[:30]],
            "emerging": [_headline(c) for c in emerging[:20]],
            "risks": [_headline(c) for c in risks[:30]],
            "opportunities": [_headline(c) for c in opportunities[:20]],
            "moves": [_headline(c) for c in moves[:20]],
            "momentum": momentum[:30],
            "regulatory": [{
                "id": c.get("id"), "domain": c.get("domain"),
                "affected_industries": c.get("affected_industries") or [],
                "current_stage": c.get("current_stage"),
                "days_to_deadline": c.get("days_to_deadline"),
                "threat_score": c.get("threat_score"),
                "blast": len(c.get("affected_industries") or []),
            } for c in sorted(reg_cards, key=lambda c: len(c.get("affected_industries") or []),
                              reverse=True)[:20]],
            "recommendations": _recommendations(cards, reg_cards, distress, momentum, labels),
        },
    }


def build_executive(feed: dict[str, Any]) -> dict[str, Any]:
    """`feed.executive` — the per-officer blocks. Read-track: CSO only (CRO/CCO/CPO next slice)."""
    return {
        "officers": ["cso"],
        "generated_at": feed.get("generated_at"),
        "as_of": feed.get("as_of"),
        "cso": build_cso(feed),
    }
