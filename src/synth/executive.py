"""ADR 021 §D/§G — the per-officer executive blocks (`feed.executive`).

Four **enriched** officer dashboards (CSO/CRO/CCO/CPO), each a DERIVED, per-industry-aware view
built entirely from fields the feed already carries — no new data, no fabrication. Every panel
item references a real card/entity id so the v3 app links to its grounding; composite scores
(e.g. the Strategic Climate Index) are transparent, documented formulas surfaced as *inferences*,
never facts.

Industry scoping (§G) is a client-side filter: each item carries `industries` (empty ⇒
sector-agnostic, shown under every sector); per-industry headline aggregates are precomputed
here under each block's `by_industry[slug]` (+ `__all__`). Defamation guardrail: distress on an
executive surface is gated to CONFIRMED confidence (`_trusted_distress`) — `reported` news
distress carries the #33 counterparty mis-attribution risk and stays off the board.
"""
from __future__ import annotations

from typing import Any

ALL = "__all__"
OFFICERS = ("cso", "cro", "cco", "cpo")

_MOVE_TOPICS = {"concorrencia", "novos_entrantes"}
_MOVE_LENSES = {"entrants", "ofertas", "market"}
_MA_CUES = ("aquisi", "fusão", "fusao", "compra", "incorpora", "joint venture", "m&a", "cade")
_REG_KINDS = ("regulatory_lifecycle", "regulatory_fusion")


# --- shared helpers -------------------------------------------------------------------
def _cards(feed: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in (feed.get("feed") or []) if isinstance(c, dict)]


def _in_industry(item: dict[str, Any], slug: str | None) -> bool:
    if not slug or slug == ALL:
        return True
    inds = set(item.get("industries") or []) | set(item.get("affected_industries") or [])
    return slug in inds


def _threat(card: dict[str, Any]) -> float:
    """Threat on a 0–100 scale (feed threat_score is 0–1)."""
    try:
        v = float(card.get("threat_score") or 0)
    except (TypeError, ValueError):
        return 0.0
    return v * 100 if v <= 1 else v


def _trusted_distress(feed: dict[str, Any]) -> list[dict[str, Any]]:
    """Distress safe for an executive/board surface: CONFIRMED confidence only. `reported`
    news distress carries #33 counterparty mis-attribution risk (a bank named in a retailer's
    RJ news must never show as itself insolvent)."""
    return [d for d in (feed.get("distress") or []) if d.get("confidence") == "confirmed"]


def _recent_window(dates: list[str]) -> tuple[set[str], set[str]]:
    ds = sorted(str(d) for d in (dates or []))
    return set(ds[-7:]), set(ds[-14:-7])


def _labels(feed: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in (feed.get("entities") or []):
        if e.get("entity"):
            out[e["entity"]] = e.get("label") or e["entity"]
    for eid, a in (feed.get("entity_attrs") or {}).items():
        out.setdefault(eid, (a or {}).get("label") or eid)
    return out


def _industries_of(feed: dict[str, Any], entity: str | None) -> list[str]:
    if not entity:
        return []
    return list(((feed.get("entity_attrs") or {}).get(entity) or {}).get("industries") or [])


def _headline(card: dict[str, Any]) -> dict[str, Any]:
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


def _climate_index(cards: list[dict[str, Any]], n_distress: int) -> int:
    """Strategic Climate Index (0–100, higher = calmer) — transparent composite, labelled an
    inference in the UI. 100 minus recent avg threat, alert ratio, and a distress penalty."""
    if not cards:
        return 50
    avg_threat = sum(_threat(c) for c in cards) / len(cards)
    alert_ratio = sum(1 for c in cards if c.get("is_alert")) / len(cards)
    penalty = 0.6 * avg_threat + 0.3 * (alert_ratio * 100) + 0.1 * min(n_distress * 20, 100)
    return max(0, min(100, round(100 - penalty)))


def _momentum(cards: list[dict[str, Any]], dates: list[str],
              labels: dict[str, str]) -> list[dict[str, Any]]:
    """Per-entity competitor momentum = recent-window avg threat − prior-window avg threat."""
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
        out.append({"entity": e, "label": labels.get(e, e), "recent": round(rec, 1),
                    "prior": round(pri, 1), "momentum": round(rec - pri, 1),
                    "industries": sorted(inds.get(e, set()))})
    out.sort(key=lambda x: x["momentum"], reverse=True)
    return out


def _by_industry(sectors: list[dict[str, str]], fn) -> dict[str, Any]:
    """Apply an aggregate builder `fn(slug)` for every sector + __all__."""
    return {slug: fn(slug) for slug in [ALL] + [s["slug"] for s in sectors]}


def _rec(horizon: str, text: str, action: str, *, officer: str, entity=None,
         evidence_id=None, industries=None) -> dict[str, Any]:
    return {"horizon": horizon, "text": text, "action": action, "officer": officer,
            "entity": entity, "evidence_id": evidence_id, "industries": industries or []}


# --- CSO (strategic) ------------------------------------------------------------------
def build_cso(feed: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    cards, dates, labels = ctx["cards"], ctx["dates"], ctx["labels"]
    recent = ctx["recent"]
    reg_cards = ctx["reg_cards"]
    distress = _trusted_distress(feed)
    news = [c for c in cards if c.get("kind") != "regulatory_lifecycle"]
    headlines = sorted(news, key=lambda c: (str(c.get("date") or ""), _threat(c)), reverse=True)
    emerging = [c for c in news if str(c.get("date") or "") in recent and _threat(c) >= 60]
    risks = sorted((c for c in cards if c.get("is_alert")), key=_threat, reverse=True)
    opps = [c for c in cards if (c.get("swot_hint") or {}).get("dimension") == "O"
            or (c.get("swot_hint") or {}).get("sign") == "-"]
    moves = [c for c in news if _is_move(c)]
    momentum = _momentum(cards, dates, labels)

    def agg(slug):
        sc = [c for c in cards if _in_industry(c, slug)]
        sreg = [c for c in reg_cards if _in_industry(c, slug)]
        sdist = [d for d in distress if slug in (ALL, None)
                 or slug in _industries_of(feed, d.get("entity"))]
        smoves = [c for c in moves if _in_industry(c, slug)]
        n = len(sc)
        return {"climate": _climate_index(sc, len(sdist)), "n_cards": n,
                "n_alerts": sum(1 for c in sc if c.get("is_alert")),
                "avg_threat": round(sum(_threat(c) for c in sc) / n, 1) if n else 0.0,
                "reg_threat": round(sum(_threat(c) for c in sreg) / len(sreg), 1) if sreg else 0.0,
                "n_moves": len(smoves), "distress": len(sdist)}

    recs = []
    named = sorted((c for c in cards if c.get("is_alert") and c.get("entity") in labels
                    and c.get("kind") not in _REG_KINDS), key=_threat, reverse=True)
    if named:
        t = named[0]
        recs.append(_rec("imediato", f"Abrir watch estratégico: {t.get('entity_label') or t.get('entity')}",
                         "open_watch", officer="cso", entity=t.get("entity"),
                         evidence_id=t.get("id"), industries=t.get("industries") or []))
    reg_sorted = sorted(reg_cards, key=lambda c: len(c.get("affected_industries") or []), reverse=True)
    if reg_sorted:
        r = reg_sorted[0]
        na = len(r.get("affected_industries") or [])
        recs.append(_rec("30d", f"Avaliar mudança regulatória — {r.get('domain') or 'regulação'} "
                         f"(afeta {na} setor{'es' if na != 1 else ''})", "open_watch",
                         officer="cso", evidence_id=r.get("id"), industries=r.get("affected_industries") or []))
    rising = [m for m in momentum if m["momentum"] > 0][:1]
    if rising:
        m = rising[0]
        recs.append(_rec("90d", f"Formular tese sobre o avanço de {m['label']} (momentum +{m['momentum']})",
                         "curate_belief", officer="cso", entity=m["entity"], industries=m["industries"]))

    return {"by_industry": _by_industry(ctx["sectors"], agg), "panels": {
        "headlines": [_headline(c) for c in headlines[:30]],
        "emerging": [_headline(c) for c in emerging[:20]],
        "risks": [_headline(c) for c in risks[:30]],
        "opportunities": [_headline(c) for c in opps[:20]],
        "moves": [_headline(c) for c in moves[:20]],
        "momentum": momentum[:30],
        "regulatory": [_reg_row(c) for c in reg_sorted[:20]],
        "recommendations": recs,
    }}


def _reg_row(c: dict[str, Any]) -> dict[str, Any]:
    cr = c.get("change_record") or {}
    return {"id": c.get("id"), "domain": c.get("domain"),
            "affected_industries": c.get("affected_industries") or [],
            "current_stage": c.get("current_stage"), "days_to_deadline": c.get("days_to_deadline"),
            "threat_score": c.get("threat_score"), "blast": len(c.get("affected_industries") or []),
            "date": c.get("date"), "n_changes": c.get("n_changes"),
            "blast_band": (cr.get("blast_radius") or {}).get("band"),
            "difficulty_band": (cr.get("difficulty") or {}).get("band"),
            "change": cr.get("change"), "impact": cr.get("impact")}


# --- CRO (regulator) ------------------------------------------------------------------
def build_cro(feed: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    reg = ctx["reg_cards"]
    timeline = sorted(reg, key=lambda c: str(c.get("date") or ""), reverse=True)
    impact = sorted((c for c in reg if c.get("change_record")),
                    key=lambda c: (c.get("change_record") or {}).get("blast_radius", {}).get("score", 0),
                    reverse=True)
    deadlines = sorted((c for c in reg if c.get("days_to_deadline") is not None),
                       key=lambda c: c.get("days_to_deadline"))
    changes = [c for c in reg if (c.get("n_changes") or 0) > 0]

    def agg(slug):
        sr = [c for c in reg if _in_industry(c, slug)]
        blasts = [len(c.get("affected_industries") or []) for c in sr]
        return {"n_reg": len(sr),
                "reg_threat": round(sum(_threat(c) for c in sr) / len(sr), 1) if sr else 0.0,
                "n_changes": sum(1 for c in sr if (c.get("n_changes") or 0) > 0),
                "n_deadlines": sum(1 for c in sr if c.get("days_to_deadline") is not None),
                "max_blast": max(blasts) if blasts else 0}

    recs = []
    if impact:
        r = impact[0]
        recs.append(_rec("imediato", f"Acompanhar norma de maior alcance — {r.get('domain') or 'regulação'}",
                         "open_watch", officer="cro", evidence_id=r.get("id"),
                         industries=r.get("affected_industries") or []))
    if deadlines:
        r = deadlines[0]
        recs.append(_rec("30d", f"Prazo em {r.get('days_to_deadline')}d — {r.get('domain') or 'regulação'}",
                         "open_watch", officer="cro", evidence_id=r.get("id"),
                         industries=r.get("affected_industries") or []))
    return {"by_industry": _by_industry(ctx["sectors"], agg), "panels": {
        "timeline": [_reg_row(c) for c in timeline[:30]],
        "impact": [_reg_row(c) for c in impact[:20]],
        "deadlines": [_reg_row(c) for c in deadlines[:20]],
        "changes": [{**_reg_row(c), "changes": (c.get("changes") or [])[:6]} for c in changes[:20]],
        "recommendations": recs,
    }}


# --- CCO (compliance) -----------------------------------------------------------------
def build_cco(feed: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    labels = ctx["labels"]
    findings = list((feed.get("integrity") or {}).get("findings") or [])
    distress = _trusted_distress(feed)
    reputation = sorted((feed.get("reputation") or []),
                        key=lambda r: (r.get("rank") if r.get("rank") is not None else 999))

    def f_inds(f):
        return _industries_of(feed, f.get("entity_id"))

    integrity_rows = [{"id": f.get("id"), "kind": f.get("kind"), "severity": f.get("severity"),
                       "summary": f.get("summary"), "entity_id": f.get("entity_id"),
                       "card_id": f.get("card_id"), "industries": f_inds(f)} for f in findings]
    rep_rows = [{"id": r.get("id"), "entity": r.get("entity"), "label": r.get("company") or r.get("entity"),
                 "index": r.get("index"), "rank": r.get("rank"), "category": r.get("category"),
                 "period": r.get("period"), "url": r.get("url"),
                 "industries": _industries_of(feed, r.get("entity"))} for r in reputation]
    # Risk register = confirmed distress + worst-reputation + high-severity integrity.
    risk_register = (
        [{"kind": "distress", "label": labels.get(d.get("entity"), d.get("entity")),
          "detail": d.get("label"), "industries": _industries_of(feed, d.get("entity")),
          "severity": "high"} for d in distress]
        + [{"kind": "reputacao", "label": r["label"], "detail": f"#{r['rank']} reclamações · índice {r['index']}",
            "industries": r["industries"], "severity": "med"} for r in rep_rows[:8]]
        + [{"kind": "integridade", "label": (i.get("entity_id") or i.get("card_id") or "—"),
            "detail": i.get("summary"), "industries": i.get("industries"), "severity": i.get("severity")}
           for i in integrity_rows if i.get("severity") == "high"]
    )

    def agg(slug):
        si = [i for i in integrity_rows if _in_industry(i, slug)]
        sd = [d for d in distress if slug in (ALL, None) or slug in _industries_of(feed, d.get("entity"))]
        sr = [r for r in rep_rows if _in_industry(r, slug)]
        return {"n_integrity": len(si), "n_distress": len(sd), "n_rep": len(sr),
                "n_high": sum(1 for i in si if i.get("severity") == "high"),
                "worst_rank": min([r["rank"] for r in sr if r.get("rank")], default=None)}

    recs = [_rec("imediato", "Rodar auditoria de integridade do registro", "run_integrity_audit",
                 officer="cco")]
    high = [i for i in integrity_rows if i.get("severity") == "high"]
    if high:
        i = high[0]
        recs.append(_rec("imediato", f"Sinalizar achado de integridade: {i.get('entity_id') or i.get('card_id')}",
                         "flag_entity", officer="cco", entity=i.get("entity_id"), industries=i.get("industries")))
    return {"by_industry": _by_industry(ctx["sectors"], agg), "panels": {
        "integrity": integrity_rows[:40],
        "risk_register": risk_register[:30],
        "reputation": rep_rows[:30],
        "recommendations": recs,
    }}


# --- CPO (product) --------------------------------------------------------------------
def build_cpo(feed: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    industries = list(feed.get("industries") or [])
    gaps = list(feed.get("coverage_gaps") or [])
    reviews = [r for r in (feed.get("reviews") or []) if r.get("kind") == "discovery"]
    rc = feed.get("regulatory_coverage") or {}
    radar_tiers: dict[str, int] = {}
    for a in (feed.get("entity_attrs") or {}).values():
        tier = ((a or {}).get("radar") or {}).get("tier") if isinstance((a or {}).get("radar"), dict) else None
        if tier:
            radar_tiers[tier] = radar_tiers.get(tier, 0) + 1

    coverage_map = [{"slug": i.get("slug"), "label": i.get("display_name") or i.get("slug"),
                     "covered": bool(i.get("covered")), "coverage_gap": bool(i.get("coverage_gap")),
                     "low_volume": bool(i.get("low_volume")), "narratives": i.get("narratives"),
                     "active_entities": i.get("active_entities"), "industries": [i.get("slug")]}
                    for i in industries]
    blind = sorted(gaps, key=lambda g: (g.get("status") != "open", -(g.get("count") or 0)))
    blind_rows = [{"id": g.get("id"), "question": g.get("question"), "count": g.get("count"),
                   "status": g.get("status"), "reason": g.get("reason"),
                   "triage": (g.get("triage") or {}).get("class"),
                   "issue_url": g.get("issue_url"), "industries": []} for g in blind]
    disc_rows = [{"id": r.get("review_id"), "proposed": r.get("proposed"), "reason": r.get("reason"),
                  "hint": r.get("hint"), "confidence": r.get("confidence"), "industries": []}
                 for r in reviews]

    def agg(slug):
        if slug == ALL:
            covered = sum(1 for i in coverage_map if i["covered"])
            cgap = sum(1 for i in coverage_map if i["coverage_gap"])
        else:
            covered = sum(1 for i in coverage_map if i["slug"] == slug and i["covered"])
            cgap = sum(1 for i in coverage_map if i["slug"] == slug and i["coverage_gap"])
        return {"n_gaps": sum(1 for g in blind_rows if g["status"] == "open"),
                "n_reviews": len(disc_rows), "n_covered": covered, "n_coverage_gap": cgap}

    recs = []
    open_gaps = [g for g in blind_rows if g["status"] == "open"]
    if open_gaps:
        g = open_gaps[0]
        recs.append(_rec("imediato", f"Triagem de ponto cego: {g['question']}", "resolve_review",
                         officer="cpo"))
    if disc_rows:
        d = disc_rows[0]
        recs.append(_rec("30d", f"Revisar proposta de descoberta: {d['proposed']}", "resolve_review",
                         officer="cpo"))
    return {"by_industry": _by_industry(ctx["sectors"], agg), "panels": {
        "coverage_map": coverage_map,
        "blind_spots": blind_rows[:30],
        "discovery": disc_rows[:30],
        "reg_coverage": {"summary": rc.get("summary") or {},
                         "entity_covered": rc.get("entity_covered") or [],
                         "signal_only": rc.get("signal_only") or [], "gap": rc.get("gap") or []},
        "radar": radar_tiers,
        "recommendations": recs,
    }}


# --- Executive Flow (§D) — incident → Trajectory → officer → briefing -----------------
import hashlib


def _tid(kind: str, key: str) -> str:
    """Stable Trajectory id (a decision's context_id links back to it across builds)."""
    return "traj-" + hashlib.sha1(f"{kind}:{key}".encode()).hexdigest()[:12]


def _traj(kind, title, officer, severity, briefing, *, industries=None, evidence_ids=None,
          action=None, action_ref=None, handoff=None, key="") -> dict[str, Any]:
    return {"id": _tid(kind, key or title), "trigger": kind, "title": title, "officer": officer,
            "severity": severity, "briefing": briefing, "industries": industries or [],
            "evidence_ids": [e for e in (evidence_ids or []) if e], "action": action,
            "action_ref": action_ref, "handoff": handoff}


_SEV_RANK = {"crit": 0, "high": 1, "med": 2}


def build_flow(feed: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """The Executive Flow: detect qualifying events → open a durable Trajectory (stable id) →
    route to the owning officer (the correlation engine) → a deterministic, cited briefing +
    the recommended catalog action. Grounded only — briefings restate the trigger's own fields,
    never invented facts. High-blast reg changes hand off CRO→CCO (compliance impact)."""
    labels, reg_cards = ctx["labels"], ctx["reg_cards"]
    out: list[dict[str, Any]] = []

    # Regulatory change with material blast-radius → CRO (hand off to CCO when market-wide).
    for c in sorted(reg_cards, key=lambda c: (c.get("change_record") or {}).get("blast_radius", {}).get("score", 0),
                    reverse=True):
        cr = c.get("change_record") or {}
        band = (cr.get("blast_radius") or {}).get("band")
        if band not in ("market", "broad", "sector"):
            continue
        sev = "crit" if band == "market" else "high"
        n_ind = len(c.get("affected_industries") or [])
        brief = (cr.get("impact") or f"Alcance {band}: afeta {n_ind} setor(es); "
                 f"dificuldade {(cr.get('difficulty') or {}).get('band') or 'n/d'}.")
        out.append(_traj("mudanca_regulatoria", f"Mudança regulatória — {c.get('domain') or 'regulação'}",
                         "cro", sev, brief, industries=c.get("affected_industries") or [],
                         evidence_ids=[c.get("id")], action="Avaliar e roteirizar a mudança",
                         action_ref="open_watch", key=c.get("id") or "",
                         handoff=("cco" if band == "market" else None)))
        if len(out) >= 4:
            break

    # Confirmed insolvency on the roster → CCO.
    for d in _trusted_distress(feed):
        e = d.get("entity")
        out.append(_traj("distress", f"Sinal de insolvência — {labels.get(e, e)}", "cco", "crit",
                         f"Processo de {d.get('label') or 'distress'} (confiança {d.get('confidence')}).",
                         industries=_industries_of(feed, e), evidence_ids=(d.get("evidence") or [])[:2],
                         action="Sinalizar para revisão de compliance", action_ref="flag_entity", key=e or ""))

    # High-severity integrity finding → CCO.
    for f in [x for x in ((feed.get("integrity") or {}).get("findings") or []) if x.get("severity") == "high"][:3]:
        out.append(_traj("integridade", f"Achado de integridade — {f.get('kind')}", "cco", "high",
                         f.get("summary") or "Anomalia de registro detectada.",
                         industries=_industries_of(feed, f.get("entity_id")),
                         evidence_ids=[f.get("card_id")], action="Rodar auditoria de integridade",
                         action_ref="run_integrity_audit", key=f.get("id") or ""))

    # Competitor gaining the most momentum → CSO.
    momentum = _momentum(ctx["cards"], ctx["dates"], labels)
    for m in [x for x in momentum if x["momentum"] >= 15][:3]:
        out.append(_traj("avanco_competitivo", f"Avanço competitivo — {m['label']}", "cso",
                         "high" if m["momentum"] >= 25 else "med",
                         f"Momentum +{m['momentum']} na janela (ameaça {m['prior']}→{m['recent']}).",
                         industries=m["industries"], action="Formular tese competitiva",
                         action_ref="curate_belief", key=m["entity"]))

    # Recurring blind spot (unanswered demand) → CPO.
    for g in sorted([x for x in (feed.get("coverage_gaps") or []) if x.get("status") == "open"],
                    key=lambda g: -(g.get("count") or 0))[:2]:
        out.append(_traj("ponto_cego", f"Ponto cego recorrente — {(g.get('question') or '')[:60]}", "cpo",
                         "med", f"Pergunta sem resposta {g.get('count') or 0}× · {(g.get('triage') or {}).get('class') or 'lacuna'}.",
                         action="Triar lacuna de cobertura", action_ref="resolve_review", key=g.get("id") or ""))

    out.sort(key=lambda t: _SEV_RANK.get(t["severity"], 9))
    return out[:14]


# --- top level ------------------------------------------------------------------------
def build_executive(feed: dict[str, Any], *, decisions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """`feed.executive` — the four enriched officer blocks + shared sectors + the Executive Flow
    (§D trajectories) + the Decision-Trust metrics (§E, from `decisions` if supplied)."""
    cards = _cards(feed)
    dates = list(feed.get("dates") or [])
    recent, _prior = _recent_window(dates)
    sectors = [{"slug": o.get("slug"), "label": o.get("display_name") or o.get("label") or o.get("slug")}
               for o in (feed.get("industry_options") or []) if o.get("slug")]
    ctx = {"cards": cards, "dates": dates, "recent": recent, "labels": _labels(feed),
           "sectors": sectors, "reg_cards": [c for c in cards if c.get("kind") in _REG_KINDS]}
    metrics = {}
    try:
        from src.synth import decision_metrics
        metrics = decision_metrics.compute_metrics(decisions or [])
    except Exception as exc:  # pragma: no cover - metrics best-effort
        print(f"Warning: decision metrics skipped: {exc}")
    return {
        "officers": list(OFFICERS),
        "generated_at": feed.get("generated_at"),
        "as_of": feed.get("as_of"),
        "sectors": sectors,
        "flow": build_flow(feed, ctx),
        "metrics": metrics,
        "cso": build_cso(feed, ctx),
        "cro": build_cro(feed, ctx),
        "cco": build_cco(feed, ctx),
        "cpo": build_cpo(feed, ctx),
    }
