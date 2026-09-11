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
        "title": (card.get("narrative") or "")[:240],
    }


_AXIS_EXTRAS = ("hub", "n_dependents", "relation", "pattern", "horizon_days")


def _axis_rows(feed: dict[str, Any], *fields: str) -> list[dict[str, Any]]:
    """Re-project narrative cards that carry a deep-axis field (SURF-6/7/11): behavioral
    (`pattern`), relational (`relation`), predictive (`horizon_days`), ecosystem (`hub`). The
    axes already ride on the cards — this just routes them to an officer panel, no new inference."""
    rows: list[dict[str, Any]] = []
    for c in _cards(feed):
        if not any(c.get(f) is not None for f in fields):
            continue
        h = _headline(c)
        for extra in _AXIS_EXTRAS:
            if c.get(extra) is not None:
                h[extra] = c.get(extra)
        rows.append(h)
    rows.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    return rows


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


def _asset_size_index(feed: dict[str, Any]) -> dict[str, float]:
    """entity_id → total assets (R$ bi, IF.data/CVM `fundamentals.ativo_bi`) — a grounded market-
    size proxy for the Mapa Competitivo bubble size. Independent of `_fundamentals_rows`' ROE
    gate: an entity can carry a balance-sheet size without a reported ROE. Only ~30% of tracked
    entities have this (listed/IF.data-covered institutions) — callers must treat a missing key
    as "no data", never default to 0 (0 would visually claim "no assets", which is false)."""
    out: dict[str, float] = {}
    for e in (feed.get("entities") or []):
        v = (e.get("fundamentals") or {}).get("ativo_bi")
        if v is not None and e.get("entity"):
            out[e["entity"]] = float(v)
    return out


def _group_roots(feed: dict[str, Any]) -> dict[str, str]:
    """{entity -> top of its ADR-017 corporate group}. Cycle-safe and depth-capped: a corrupt
    parent loop resolves to the entity itself rather than spinning."""
    attrs = feed.get("entity_attrs") or {}
    out: dict[str, str] = {}
    for eid in attrs:
        cur, seen = eid, {eid}
        for _ in range(16):
            nxt = (attrs.get(cur) or {}).get("parent")
            if not nxt or nxt not in attrs or nxt in seen:
                break
            seen.add(nxt)
            cur = str(nxt)
        if cur != eid:
            out[eid] = cur
    return out


def _momentum(cards: list[dict[str, Any]], dates: list[str],
              labels: dict[str, str], *, sizes: dict[str, float] | None = None,
              rollup: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Per-entity competitor momentum = recent-window avg threat − prior-window avg threat.

    With `rollup` ({child -> group root}), a conglomerate's sub-entities aggregate INTO the
    parent brand so the Mapa Competitivo draws one dot per competitor rather than one per
    legal entity (live 2026-09-11: BTG drew 8 dots, Itaú 7, XP 5). The rolled row carries the
    union of its members' industries, so a group shows up on every sector map it actually
    operates in — through its corretora on investment-banking, its funds on real-estate-funds
    — which is the honest read of a conglomerate's competitive footprint.
    """
    recent, prior = _recent_window(dates)
    roll = rollup or {}
    acc: dict[str, dict[str, list[float]]] = {}
    inds: dict[str, set[str]] = {}
    for c in cards:
        e = c.get("entity")
        if not e:
            continue
        e = roll.get(e, e)
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
                    "industries": sorted(inds.get(e, set())),
                    # None (not 0) when unknown — the client must fall back to a default
                    # bubble size, never render "no data" as "zero assets".
                    "size_bi": (sizes or {}).get(e)})
    out.sort(key=lambda x: x["momentum"], reverse=True)
    return out


def _momentum_for_panel(rows: list[dict[str, Any]], sectors: list[dict[str, str]], *,
                        per_sector: int = 24, overall: int = 30) -> list[dict[str, Any]]:
    """Select the momentum rows the CSO panels ship, WITHOUT starving any one sector.

    The panel payload feeds a client that filters per sector, so a blind global `rows[:30]`
    is wrong twice over:
      1. It caps ACROSS sectors — one busy sector's rows crowd every other sector's map down
         to a handful of dots (live 2026-09-09: banking had 17 eligible entities, 4 survived).
      2. `_momentum` sorts by SIGNED momentum, so a global head-slice deletes the whole
         declining tail — every incumbent losing threat (Itaú, BB, Bradesco, Santander,
         Nubank were all cut). The scatter's x-axis is labelled "recua ← 0 → acelera", so
         truncating one side makes half the chart structurally unreachable.

    Fix: rank by |momentum| (the biggest movers in EITHER direction are the story), then ship
    the union of the top `overall` globally and the top `per_sector` within each sector. Bounded
    by activity (only entities with a card in the recent window get a row at all), sector-fair,
    and sign-symmetric. Ties at 0.0 keep `_momentum`'s signed order underneath for stability.
    """
    ranked = sorted(rows, key=lambda x: (abs(x.get("momentum") or 0), x.get("momentum") or 0),
                    reverse=True)
    keep: dict[str, dict[str, Any]] = {r["entity"]: r for r in ranked[:overall] if r.get("entity")}
    for s in sectors or []:
        slug = s.get("slug")
        if not slug:
            continue
        n = 0
        for r in ranked:
            if slug in (r.get("industries") or []):
                keep.setdefault(r["entity"], r)
                n += 1
                if n >= per_sector:
                    break
    return [r for r in ranked if r.get("entity") in keep]


def _by_industry(sectors: list[dict[str, str]], fn) -> dict[str, Any]:
    """Apply an aggregate builder `fn(slug)` for every sector + __all__."""
    return {slug: fn(slug) for slug in [ALL] + [s["slug"] for s in sectors]}


def _rec(horizon: str, text: str, action: str, *, officer: str, entity=None,
         evidence_id=None, industries=None) -> dict[str, Any]:
    return {"horizon": horizon, "text": text, "action": action, "officer": officer,
            "entity": entity, "evidence_id": evidence_id, "industries": industries or []}


# --- CSO weekly brief (pilot-persona loop) --------------------------------------------
# The CSO's job is a WEEKLY ritual about DELTA — "what changed in my competitive landscape
# and what decision does it invite" — not a live panel scan. `build_cso_weekly` synthesizes
# exactly that from the fields the feed already carries (dated cards + the recent/prior windows
# executive already computes): a plain-language headline, week-over-week metric deltas, the
# week's material bucketed into the CSO's decision categories, and the top-3 priorities each
# paired with the decision it invites. Pure derivation — no new data, no LLM, no fabrication.
# A "move" is a pricing/competitive act (rate cut, new offer, M&A) — substantive content that
# should win over a bare entrant tag. Note freshly-discovered sub-entities (consórcio/corretora)
# inherit `entrants`/`novos_entrantes` on EVERY card incl. a rate story, so entrant is the
# lowest-priority classification, used only when nothing more specific fits.
_MOVE_LENSES_CLS = {"juros", "ofertas", "market", "pricing"}
_ENTRANT_LENSES = {"entrants"}
_ENTRANT_TOPICS = {"novos_entrantes"}


def _is_entrant(c: dict[str, Any]) -> bool:
    return bool(set(c.get("lenses") or []) & _ENTRANT_LENSES
                or set(c.get("topics") or []) & _ENTRANT_TOPICS)


def _blast(c: dict[str, Any]) -> int:
    return len(c.get("affected_industries") or c.get("industries") or [])


def _delta(now: int, was: int) -> dict[str, int]:
    return {"now": now, "prior": was, "delta": now - was}


def _priority_class(c: dict[str, Any]) -> str:
    """Classify a week-priority card by its dominant signal — the SAME order drives both the
    `so_what` label and the decision it invites, so they never disagree. Substance (reg, then a
    pricing/M&A move) wins over the entrant tag freshly-discovered sub-entities carry everywhere."""
    if c.get("kind") in _REG_KINDS:
        return "regulatory"
    if set(c.get("lenses") or []) & _MOVE_LENSES_CLS or any(
            cue in (c.get("narrative") or "").lower() for cue in _MA_CUES):
        return "move"
    if _is_entrant(c):
        return "entrant"
    if c.get("is_alert"):
        return "alert"
    return "move"


_CLASS_LABEL = {"regulatory": "Mudança regulatória", "move": "Movimento competitivo",
                "entrant": "Novo entrante", "alert": "Alerta ativo"}

# A bare BCB-juros rate-table update is real signal (correctly counted as a "move" for metrics/
# buckets above) but HIGH VOLUME and routine — on the live feed it was crowding the weekly top-3
# out with automated rate blips, leaving no room for the low-volume, high-SIGNAL events (M&A,
# entrants, regulatory, genuine alerts) a CSO actually needs surfaced. This tier is used ONLY to
# rank the top-3 — it does not change `_priority_class`, so metrics/buckets/so_what/decision stay
# exactly as before (no scope creep into the honest volume counts).
_SUBSTANTIVE_LENSES = {"ofertas", "market", "pricing"}


def _is_commodity_rate_update(c: dict[str, Any]) -> bool:
    """A juros card is commodity unless it ALSO carries a genuinely substantive lens, an M&A
    cue, or is independently alert-worthy. Deliberately ignores the `entrants` lens/topic here:
    ADR-017 sub-entity discovery stamps `entrants`/`novos_entrantes` on every card for a fresh
    sub-entity, including its routine rate prints — that tag is known-unreliable noise on a
    juros card (the same finding that motivated `_priority_class`'s own reg→move precedence),
    so it must not be allowed to smuggle a rate blip past the commodity check."""
    lenses = set(c.get("lenses") or [])
    if "juros" not in lenses or lenses & _SUBSTANTIVE_LENSES:
        return False
    if c.get("is_alert"):
        return False  # a genuinely alert-worthy rate move still competes on its own merits
    return not any(cue in (c.get("narrative") or "").lower() for cue in _MA_CUES)


def _priority_rank_tier(c: dict[str, Any]) -> int:
    return 0 if _is_commodity_rate_update(c) else 1


def _priority_decision(c: dict[str, Any]) -> dict[str, Any]:
    """Map a week-priority card to the decision it invites (reuses the officer action catalog)."""
    label = c.get("entity_label") or c.get("entity") or "concorrente"
    cls = _priority_class(c)
    if cls == "regulatory":
        na = _blast(c)
        return _rec("30d", f"Avaliar impacto regulatório — {c.get('domain') or 'regulação'} "
                    f"(afeta {na} setor{'es' if na != 1 else ''})", "open_watch", officer="cso",
                    evidence_id=c.get("id"), industries=c.get("affected_industries") or [])
    if cls == "entrant":
        return _rec("30d", f"Dimensionar novo entrante: {label}", "curate_belief", officer="cso",
                    entity=c.get("entity"), evidence_id=c.get("id"), industries=c.get("industries") or [])
    if cls == "alert":
        return _rec("imediato", f"Abrir watch estratégico: {label}", "open_watch", officer="cso",
                    entity=c.get("entity"), evidence_id=c.get("id"), industries=c.get("industries") or [])
    return _rec("90d", f"Formular tese sobre o movimento de {label}", "curate_belief", officer="cso",
                entity=c.get("entity"), evidence_id=c.get("id"), industries=c.get("industries") or [])


def _story_key(c: dict[str, Any]):
    """Same-story signature: identical source_ids ⇒ the same underlying event, re-emitted per
    sub-entity (ADR-017). Falls back to the card id when a card has no source_ids."""
    sids = tuple(sorted(str(s) for s in (c.get("source_ids") or [])))
    return sids or ("__id__", c.get("id"))


def _dedup_by_story(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse cards that share a source signature, keeping the highest-signal representative
    (alert, then threat) — so one event counts once in the brief, not once per sub-entity."""
    best: dict[Any, dict[str, Any]] = {}
    for c in cards:
        k = _story_key(c)
        cur = best.get(k)
        if cur is None or (bool(c.get("is_alert")), _threat(c)) > (bool(cur.get("is_alert")), _threat(cur)):
            best[k] = c
    return list(best.values())


def _weekly_headline(m: dict[str, Any], top: list[dict[str, Any]]) -> str:
    """One deterministic plain-PT sentence the CSO can read in five seconds."""
    def _d(k: str) -> str:
        d = m[k]["delta"]
        return f" ({d:+d} vs. semana anterior)" if d else ""
    parts = [f"{m['moves']['now']} movimento(s) de concorrentes{_d('moves')}",
             f"{m['entrants']['now']} novo(s) entrante(s){_d('entrants')}",
             f"{m['regulatory']['now']} mudança(s) regulatória(s){_d('regulatory')}"]
    lead = "Semana calma no cenário competitivo." if not top else \
        f"Prioridade: {top[0]['title'][:120]}"
    return f"Nesta semana: {', '.join(parts)}. {lead}"


def build_cso_weekly(feed: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    cards, recent, prior = ctx["cards"], ctx["recent"], ctx["prior"]
    distress = _trusted_distress(feed)

    def _for(slug: str | None) -> dict[str, Any]:
        # Dedup by story up front so every count/bucket/priority is EVENT-level, not card-level —
        # ADR-017 re-emits one event per sub-entity, which would otherwise inflate the brief.
        rc = _dedup_by_story([c for c in cards if _in_industry(c, slug) and str(c.get("date") or "") in recent])
        pc = _dedup_by_story([c for c in cards if _in_industry(c, slug) and str(c.get("date") or "") in prior])
        sdist = [d for d in distress if slug in (ALL, None)
                 or slug in _industries_of(feed, d.get("entity"))]

        def _cls_of(cs, cl):
            return [c for c in cs if _priority_class(c) == cl]

        moves, entrants = _cls_of(rc, "move"), _cls_of(rc, "entrant")
        reg = [c for c in rc if c.get("kind") in _REG_KINDS]
        openings = [c for c in rc if c.get("kind") not in _REG_KINDS
                    and (c.get("swot_hint") or {}).get("dimension") in ("O", "T")]
        metrics = {
            "moves": _delta(len(moves), len(_cls_of(pc, "move"))),
            "entrants": _delta(len(entrants), len(_cls_of(pc, "entrant"))),
            "regulatory": _delta(len(reg), sum(1 for c in pc if c.get("kind") in _REG_KINDS)),
            "alerts": _delta(sum(1 for c in rc if c.get("is_alert")), sum(1 for c in pc if c.get("is_alert"))),
            "climate": _delta(_climate_index(rc, len(sdist)), _climate_index(pc, len(sdist))),
            "n_cards": _delta(len(rc), len(pc)),
        }
        # top priorities: substance (tier) first, then alert, then threat, then blast radius —
        # a routine commodity rate update never outranks a named strategic move on raw threat alone.
        ranked = sorted(rc, key=lambda c: (_priority_rank_tier(c), bool(c.get("is_alert")), _threat(c), _blast(c)),
                        reverse=True)
        top = []
        for c in ranked[:3]:
            h = _headline(c)
            h["so_what"] = _CLASS_LABEL.get(_priority_class(c), "Movimento competitivo")
            h["decision"] = _priority_decision(c)
            top.append(h)
        return {"metrics": metrics, "headline": _weekly_headline(metrics, top),
                "top_priorities": top,
                "buckets": {"moves": [_headline(c) for c in moves[:10]],
                            "entrants": [_headline(c) for c in entrants[:10]],
                            "regulatory": [_reg_row(c) for c in reg[:10]],
                            "openings": [_headline(c) for c in openings[:10]]}}

    return {"by_industry": _by_industry(ctx["sectors"], _for),
            "window": {"recent": sorted(recent), "prior": sorted(prior)}}


# --- CSO (strategic) ------------------------------------------------------------------
def _fundamentals_rows(feed: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-entity financial strength (ADR 022 Tier-1) from feed.entities[].fundamentals — ROE/ROA/
    leverage/funding/headroom + market share of crédito & lucro. Strongest ROE first. Inference."""
    labels = _labels(feed)
    keys = ("roe_pct", "roa_pct", "leverage", "credito_captacoes_pct", "basileia_headroom_pp",
            "carteira_share_pct", "lucro_share_pct", "ativo_bi", "lucro_bi", "base_date")
    rows: list[dict[str, Any]] = []
    for e in (feed.get("entities") or []):
        fu = e.get("fundamentals") or {}
        if fu.get("roe_pct") is None:
            continue
        res = e.get("resultados") or {}      # ADR 022 Tier-3: operating efficiency (opex/ativo)
        rows.append({"entity": e.get("entity"),
                     "label": e.get("label") or labels.get(e.get("entity")) or e.get("entity"),
                     "industries": e.get("industries") or _industries_of(feed, e.get("entity")),
                     "opex_ativo_pct": res.get("opex_ativo_pct"),
                     **{k: fu.get(k) for k in keys}})
    rows.sort(key=lambda r: r["roe_pct"], reverse=True)
    return rows


_TOWS_LABELS = {"SO": "Maximizar (SO)", "ST": "Contra-atacar (ST)",
                "WO": "Superar (WO)", "WT": "Evitar (WT)"}


def _posture_rows(feed: dict[str, Any]) -> list[dict[str, Any]]:
    """CSO strategic-posture panel (SURF-1): route the ADR-006 framework belief store
    (`feed.swot` counts + `feed.tows` postures) into the executive block. Curated, evidence-linked
    — a re-projection of existing vetted beliefs, NOT new inference. Entities with the most
    strategic content (TOWS postures, then SWOT bullets) lead."""
    swot = feed.get("swot") or {}
    tows = feed.get("tows") or {}
    labels = _labels(feed)
    rows: list[dict[str, Any]] = []
    for ent in set(swot) | set(tows):
        sb = swot.get(ent) or {}
        counts = sb.get("counts") or {}
        dims = sb.get("dimensions") or {}
        postures = [{"dimension": b.get("dimension"),
                     "label": _TOWS_LABELS.get(b.get("dimension"), b.get("dimension")),
                     "text": b.get("text"), "confidence": b.get("confidence")}
                    for b in (tows.get(ent) or []) if b.get("status") in (None, "active")]
        postures.sort(key=lambda x: x.get("confidence") or 0, reverse=True)

        def _top(d: str) -> str | None:
            arr = [x for x in (dims.get(d) or []) if x.get("status") == "active"]
            return arr[0].get("text") if arr else None

        if not counts and not postures:
            continue
        rows.append({
            "entity": ent,
            "label": sb.get("label") or labels.get(ent) or str(ent).replace("_", " ").title(),
            "industries": _industries_of(feed, ent),
            "counts": {d: int(counts.get(d) or 0) for d in ("S", "W", "O", "T")},
            "postures": postures[:4],
            "top": {d: _top(d) for d in ("S", "W", "O", "T")},
        })
    rows.sort(key=lambda r: (len(r["postures"]), sum(r["counts"].values())), reverse=True)
    return rows


def _framework_rows(feed: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Route a curated ADR-006 framework belief set (`feed.<key>` = {entity: [bullets]}) into an
    officer panel: per-entity rows of the vetted, evidence-linked bullets (SURF-2/3/4). Pure
    re-projection — the frameworks already gather their OWN narrative evidence, no new inference."""
    fw = feed.get(key) or {}
    labels = _labels(feed)
    rows: list[dict[str, Any]] = []
    for ent, bullets in fw.items():
        active = [{"dimension": b.get("dimension"), "text": b.get("text"),
                   "confidence": b.get("confidence")}
                  for b in (bullets or []) if b.get("status") in (None, "active")]
        if not active:
            continue
        active.sort(key=lambda x: x.get("confidence") or 0, reverse=True)
        rows.append({"entity": ent,
                     "label": labels.get(ent) or str(ent).replace("_", " ").title(),
                     "industries": _industries_of(feed, ent), "bullets": active[:6]})
    rows.sort(key=lambda r: len(r["bullets"]), reverse=True)
    return rows


def build_cso(feed: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    cards, dates, labels = ctx["cards"], ctx["dates"], ctx["labels"]
    financials = _fundamentals_rows(feed)
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
    momentum = _momentum(cards, dates, labels, sizes=_asset_size_index(feed),
                         rollup=_group_roots(feed))

    def agg(slug):
        sc = [c for c in cards if _in_industry(c, slug)]
        sreg = [c for c in reg_cards if _in_industry(c, slug)]
        sdist = [d for d in distress if slug in (ALL, None)
                 or slug in _industries_of(feed, d.get("entity"))]
        smoves = [c for c in moves if _in_industry(c, slug)]
        sfin = [r for r in financials if slug in (r.get("industries") or [])]
        roes = [r["roe_pct"] for r in sfin if r.get("roe_pct") is not None]
        n = len(sc)
        return {"climate": _climate_index(sc, len(sdist)), "n_cards": n,
                "n_alerts": sum(1 for c in sc if c.get("is_alert")),
                "avg_threat": round(sum(_threat(c) for c in sc) / n, 1) if n else 0.0,
                "reg_threat": round(sum(_threat(c) for c in sreg) / len(sreg), 1) if sreg else 0.0,
                "n_moves": len(smoves), "distress": len(sdist),
                # ADR 022 Tier-1: financial strength of the sector's tracked competitors.
                "avg_roe": round(sum(roes) / len(roes), 1) if roes else None,
                "n_negative_roe": sum(1 for r in sfin if (r.get("roe_pct") or 0) < 0)}

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
    # financial strength: the profitability leader → competitive benchmark; a loss-maker → thesis.
    leader = sorted(financials, key=lambda r: (r.get("lucro_share_pct") or 0), reverse=True)[:1]
    if leader:
        w = leader[0]
        recs.append(_rec("estrategico", f"Referência competitiva: {w['label']} lidera rentabilidade "
                         f"(ROE {w['roe_pct']}% · {w['lucro_share_pct']}% do lucro do setor)",
                         "curate_belief", officer="cso", entity=w.get("entity"), industries=w.get("industries") or []))
    losers = [r for r in financials if (r.get("roe_pct") or 0) < 0]
    if losers:
        w = losers[0]
        recs.append(_rec("90d", f"Tese sobre fragilidade de {w['label']} — ROE {w['roe_pct']}% "
                         f"(alavancagem {w.get('leverage')}x, headroom {w.get('basileia_headroom_pp')}pp)",
                         "curate_belief", officer="cso", entity=w.get("entity"), industries=w.get("industries") or []))

    return {"by_industry": _by_industry(ctx["sectors"], agg),
            # pilot-persona loop: the delta-framed weekly brief that turns the CSO board into a
            # ritual (what changed this week + the decision each invites).
            "weekly": build_cso_weekly(feed, ctx), "panels": {
        "headlines": [_headline(c) for c in headlines[:30]],
        "emerging": [_headline(c) for c in emerging[:20]],
        "risks": [_headline(c) for c in risks[:30]],
        "opportunities": [_headline(c) for c in opps[:20]],
        "moves": [_headline(c) for c in moves[:20]],
        "momentum": _momentum_for_panel(momentum, ctx["sectors"]),
        "regulatory": [_reg_row(c) for c in reg_sorted[:20]],
        # ADR 022 Tier-1: competitor financial strength (ROE/ROA/leverage/headroom/share), inference.
        "financials": financials[:25],
        # SURF-1: strategic posture — SWOT beliefs + TOWS postures routed to the CSO.
        "posture": _posture_rows(feed)[:24],
        # SURF-2: competitive frameworks (Porter five-forces + Four Corners) routed to the CSO.
        "porter": _framework_rows(feed, "porter")[:20],
        "four_corners": _framework_rows(feed, "four_corners")[:20],
        # SURF-7: forward-look — predictive (horizon) + ecosystem (infrastructure hubs), inference.
        "forward_look": _axis_rows(feed, "horizon_days", "hub")[:24],
        # SURF-11: behavioral patterns (drumbeat / multi-front) — peer-cohort read.
        "behavioral": _axis_rows(feed, "pattern")[:20],
        # SURF-6: relational graph (co-mention / convergence / dispute), review-gated upstream.
        "relational": _axis_rows(feed, "relation")[:20],
        "recommendations": recs,
    }}


def _reg_row(c: dict[str, Any]) -> dict[str, Any]:
    cr = c.get("change_record") or {}
    # The headline: the change summary if the card has one, else its own narrative (most reg
    # cards — BCB Comunicados/fusions — carry only a narrative, no change_record). Never blank.
    title = cr.get("change") or (c.get("narrative") or "")[:240]
    return {"id": c.get("id"), "domain": c.get("domain") or c.get("subject_label"),
            "title": title,
            "affected_industries": c.get("affected_industries") or [],
            "current_stage": c.get("current_stage"), "days_to_deadline": c.get("days_to_deadline"),
            "threat_score": c.get("threat_score"), "blast": len(c.get("affected_industries") or []),
            "date": c.get("date"), "n_changes": c.get("n_changes"),
            "blast_band": (cr.get("blast_radius") or {}).get("band"),
            "difficulty_band": (cr.get("difficulty") or {}).get("band"),
            "change": cr.get("change"), "impact": cr.get("impact")}


def _change_diff_rows(feed: dict[str, Any], reg_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """SURF-10: the compliance change-diff — reg cards with ENUMERATED article-level changes
    (reg_change/reg_diff) + the obligation deadline, for the CCO. Distinct from the CRO's
    blast-radius risk framing: this is 'what obligations changed, by when'."""
    rows = []
    for c in reg_cards:
        changes = c.get("changes") or []
        if not changes and not c.get("n_changes"):
            continue
        cr = c.get("change_record") or {}
        rows.append({
            "id": c.get("id"), "domain": c.get("domain") or c.get("subject_label"),
            "title": cr.get("change") or (c.get("narrative") or "")[:200],
            "n_changes": c.get("n_changes") or len(changes),
            "changes": [{"art": ch.get("art"), "verb": ch.get("verb")} for ch in changes[:8]],
            "deadline": c.get("deadline"), "days_to_deadline": c.get("days_to_deadline"),
            "difficulty_band": (cr.get("difficulty") or {}).get("band"),
            "industries": c.get("affected_industries") or [],
        })
    rows.sort(key=lambda r: (r.get("n_changes") or 0), reverse=True)
    return rows


_WEAK_BANDS = ("frágil", "atenção")
_PDD_SLOPE_WARN_PCT = 5.0   # MoM rise in loan-loss provisions that flags credit deterioration


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _fragility(row: dict[str, Any]) -> dict[str, Any] | None:
    """ADR 022 #16 — composite fragility (0–100, higher = more fragile) over the credit-risk signals
    we already hold: low Basileia, high NPL, rising PDD, weak ROE, high leverage. A transparent
    weighted average of the sub-scores that are present, renormalised to their weight (honest partial
    — like ETS). Inference. Needs ≥2 components."""
    bas, npl, pdd = row.get("indice_basileia"), row.get("npl_total"), row.get("pdd_mom_pct")
    roe, lev = row.get("roe_pct"), row.get("leverage")
    comps: list[tuple[float, float]] = []       # (weight, subscore 0–1, higher=worse)
    if bas is not None:  comps.append((0.30, _clamp((11.0 - bas) / 6.0, 0, 1)))   # <11% → fragile
    if npl is not None:  comps.append((0.25, _clamp(npl / 12.0, 0, 1)))           # 12%+ → 1
    if pdd is not None:  comps.append((0.20, _clamp(pdd / 15.0, 0, 1)))           # +15%/mo → 1
    if roe is not None:  comps.append((0.15, _clamp((10.0 - roe) / 25.0, 0, 1)))  # ROE −15% → 1
    if lev is not None:  comps.append((0.10, _clamp((lev - 12.0) / 18.0, 0, 1)))  # 30x → 1
    if len(comps) < 2:
        return None
    score = round(100 * sum(w * s for w, s in comps) / sum(w for w, _ in comps))
    band = "frágil" if score >= 60 else ("atenção" if score >= 33 else "resiliente")
    return {"score": score, "band": band, "n_components": len(comps)}


def _tone_divergence(row: dict[str, Any]) -> dict[str, Any] | None:
    """ADR 022 #17 — tone-vs-numbers divergence. FinBERT tone (−1..1) vs a numbers-health index
    (−1..1) from ROE/NPL/PDD. A large positive gap = narrative more upbeat than the numbers
    (credibility/spin watch); a large negative gap = numbers better than the tone. Inference."""
    tone = row.get("financial_tone_net")
    roe, npl, pdd = row.get("roe_pct"), row.get("npl_total"), row.get("pdd_mom_pct")
    parts: list[float] = []
    if roe is not None:  parts.append(_clamp((roe - 8.0) / 12.0, -1, 1))    # ROE 8%→0, 20%→+1, −4%→−1
    if npl is not None:  parts.append(-_clamp((npl - 4.0) / 6.0, -1, 1))    # NPL 4%→0, 10%→−1
    if pdd is not None:  parts.append(-_clamp(pdd / 10.0, -1, 1))           # rising PDD → negative
    if tone is None or not parts:
        return None
    nh = sum(parts) / len(parts)
    div = round(tone - nh, 2)
    flag = ("otimismo desalinhado" if div >= 0.4 else
            ("pessimismo desalinhado" if div <= -0.4 else None))
    return {"numbers_health": round(nh, 2), "divergence": div, "flag": flag}


def _solvency_rows(feed: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-entity prudential solvency (ADR 022 Tier A) from feed.entities[].soundness, merged with
    the Tier-B monthly balancete **slope** (feed.entities[].balancete). Weakest (lowest Índice de
    Basileia) first — the CRO's competitor-soundness view. `slope_warning` = provisions (PDD) rising
    MoM ≥ 5% — the leading indicator that moves months before the quarterly ratio."""
    labels = _labels(feed)
    rows: list[dict[str, Any]] = []
    for e in (feed.get("entities") or []):
        s = e.get("soundness") or {}
        bt = e.get("balancete") or {}
        ftone = e.get("financial_tone") or {}
        ina = e.get("inadimplencia") or {}
        fu = e.get("fundamentals") or {}      # #16/#17: ROE + leverage
        km1 = e.get("pilar3_km1") or {}       # multi-bank Pilar 3: LCR/NSFR (liquidity)
        has_solv = s.get("indice_basileia") is not None
        pdd_mom = bt.get("pdd_mom_pct")
        cred_mom = bt.get("credito_mom_pct")
        slope_warn = pdd_mom is not None and pdd_mom >= _PDD_SLOPE_WARN_PCT
        if not has_solv and not bt and not ina and not km1:
            continue
        rows.append({
            "entity": e.get("entity"),
            "label": e.get("label") or labels.get(e.get("entity")) or e.get("entity"),
            "indice_basileia": s.get("indice_basileia"),
            "capital_nivel_i": s.get("capital_nivel_i"),
            "capital_principal": s.get("capital_principal"),
            "razao_alavancagem": s.get("razao_alavancagem"),
            "band": s.get("band"),
            "base_date": s.get("base_date"),
            # Tier-B slope (monthly): rising PDD / moving crédito, months = points in series
            "pdd_mom_pct": pdd_mom,
            "credito_mom_pct": cred_mom,
            "balancete_month": bt.get("month"),
            "balancete_months": bt.get("months"),
            "slope_warning": slope_warn,
            # ADR 022 Phase 5 (inference): FinBERT tone + which corpus it read.
            "financial_tone_net": ftone.get("net"),
            "tone_corpus": ftone.get("corpus"),
            # ADR 022 Tier-2: inadimplência / NPL (15+ dias) + PF/PJ split + band.
            "npl_total": ina.get("npl_total"),
            "npl_pf": ina.get("npl_pf"),
            "npl_pj": ina.get("npl_pj"),
            "npl_band": ina.get("band"),
            "roe_pct": fu.get("roe_pct"),
            "leverage": fu.get("leverage"),
            # Multi-bank Pilar 3 KM1: LCR/NSFR (liquidity — the ADR §1 gap) + band.
            "lcr_pct": km1.get("lcr_pct"),
            "nsfr_pct": km1.get("nsfr_pct"),
            "lcr_band": km1.get("band_lcr"),
            "industries": e.get("industries") or _industries_of(feed, e.get("entity")),
        })
    # #16 composite fragility + #17 tone-vs-numbers divergence (derived from the row's own fields)
    for r in rows:
        r["fragility"] = _fragility(r)
        r["tone_divergence"] = _tone_divergence(r)
    # weakest capital first; entities with only a slope (no Basileia) sink to the end
    rows.sort(key=lambda r: (r["indice_basileia"] is None, r["indice_basileia"] if r["indice_basileia"] is not None else 0))
    return rows


# --- CRO (regulator) ------------------------------------------------------------------
def build_cro(feed: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    reg = ctx["reg_cards"]
    solvency = _solvency_rows(feed)
    weak_solvency = [r for r in solvency if r.get("band") in _WEAK_BANDS]
    # Tier-B slope firing: competitors whose provisions are rising fastest MoM (credit deterioration).
    slope_warnings = sorted((r for r in solvency if r.get("slope_warning")),
                            key=lambda r: r.get("pdd_mom_pct") or 0, reverse=True)
    # Tier-2 asset-quality firing: highest inadimplência in the "elevada" band.
    npl_high = sorted((r for r in solvency if r.get("npl_band") == "elevada"),
                      key=lambda r: r.get("npl_total") or 0, reverse=True)
    # #16 composite fragility (most fragile first) + #17 optimism-divergence (tone > numbers).
    fragile = sorted((r for r in solvency if (r.get("fragility") or {}).get("band") == "frágil"),
                     key=lambda r: (r.get("fragility") or {}).get("score", 0), reverse=True)
    optimism = sorted((r for r in solvency if (r.get("tone_divergence") or {}).get("flag") == "otimismo desalinhado"),
                      key=lambda r: (r.get("tone_divergence") or {}).get("divergence", 0), reverse=True)
    # Multi-bank Pilar 3: lowest LCR below the comfort band (liquidity watch).
    lcr_low = sorted((r for r in solvency if r.get("lcr_band") in ("crítica", "atenção")),
                     key=lambda r: r.get("lcr_pct") or 999)
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
        ss = [r for r in solvency if slug in (r.get("industries") or [])]
        npls = [r["npl_total"] for r in ss if r.get("npl_total") is not None]
        return {"n_reg": len(sr),
                "reg_threat": round(sum(_threat(c) for c in sr) / len(sr), 1) if sr else 0.0,
                "n_changes": sum(1 for c in sr if (c.get("n_changes") or 0) > 0),
                "n_deadlines": sum(1 for c in sr if c.get("days_to_deadline") is not None),
                "max_blast": max(blasts) if blasts else 0,
                # ADR 022 Phase 4: prudential solvency of the sector's tracked competitors.
                "min_basileia": min((r["indice_basileia"] for r in ss if r["indice_basileia"] is not None), default=None),
                "n_weak_solvency": sum(1 for r in ss if r.get("band") in _WEAK_BANDS),
                "n_slope_warning": sum(1 for r in ss if r.get("slope_warning")),
                # ADR 022 Tier-2: sector asset quality (max/avg inadimplência).
                "max_npl": max(npls) if npls else None,
                "n_npl_elevada": sum(1 for r in ss if r.get("npl_band") == "elevada"),
                # ADR 022 #16: sector composite fragility (worst score + count frágil).
                "max_fragility": max([(r.get("fragility") or {}).get("score") for r in ss
                                      if r.get("fragility")], default=None),
                "n_fragil": sum(1 for r in ss if (r.get("fragility") or {}).get("band") == "frágil")}

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
    if weak_solvency:
        w = weak_solvency[0]
        recs.append(_rec("imediato",
                         f"Solidez sob {w['band']} — {w['label']} (Basileia {w['indice_basileia']}%)",
                         "open_watch", officer="cro", evidence_id=w.get("entity"),
                         industries=w.get("industries") or []))
    if slope_warnings:
        w = slope_warnings[0]
        recs.append(_rec("imediato",
                         f"Deterioração de crédito — {w['label']}: provisões (PDD) +{w['pdd_mom_pct']}% no mês",
                         "open_watch", officer="cro", evidence_id=w.get("entity"),
                         industries=w.get("industries") or []))
    if npl_high:
        w = npl_high[0]
        recs.append(_rec("imediato",
                         f"Inadimplência elevada — {w['label']}: {w['npl_total']}% da carteira (15+ dias; "
                         f"PF {w.get('npl_pf')}% · PJ {w.get('npl_pj')}%)",
                         "open_watch", officer="cro", evidence_id=w.get("entity"),
                         industries=w.get("industries") or []))
    if fragile:
        w = fragile[0]
        recs.append(_rec("imediato",
                         f"Fragilidade composta — {w['label']}: índice {w['fragility']['score']}/100 "
                         f"(capital·NPL·PDD·ROE·alavancagem)",
                         "open_watch", officer="cro", evidence_id=w.get("entity"),
                         industries=w.get("industries") or []))
    if optimism:
        w = optimism[0]
        recs.append(_rec("30d",
                         f"Tom vs números — {w['label']}: relato mais otimista que os indicadores "
                         f"(divergência +{w['tone_divergence']['divergence']}) — verificar credibilidade",
                         "curate_belief", officer="cro", evidence_id=w.get("entity"),
                         industries=w.get("industries") or []))
    if lcr_low:
        w = lcr_low[0]
        recs.append(_rec("30d",
                         f"Liquidez sob {w['lcr_band']} — {w['label']}: LCR {w['lcr_pct']}% (Pilar 3)",
                         "open_watch", officer="cro", evidence_id=w.get("entity"),
                         industries=w.get("industries") or []))
    return {"by_industry": _by_industry(ctx["sectors"], agg), "panels": {
        "timeline": [_reg_row(c) for c in timeline[:30]],
        "impact": [_reg_row(c) for c in impact[:20]],
        "deadlines": [_reg_row(c) for c in deadlines[:20]],
        "changes": [{**_reg_row(c), "changes": (c.get("changes") or [])[:6]} for c in changes[:20]],
        # ADR 022 Phase 4: prudential solvency (Índice de Basileia et al.), weakest first.
        "solvency": solvency[:25],
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
        # SURF-3: PESTLE macro/regulatory environment routed to the CCO (7S dropped — internal,
        # no external signal to ground it).
        "pestle": _framework_rows(feed, "pestle")[:20],
        # SURF-10: compliance change-diff — enumerated article changes + deadlines.
        "change_diff": _change_diff_rows(feed, ctx["reg_cards"])[:24],
        "recommendations": recs,
    }}


# discovery proposals carry their SOURCE in the hint prefix — the sector signal for §G scoping.
_DISCOVERY_SOURCE_IND = {
    "cvm_fiagro": ["agri-funds"], "bcb_consorcio": ["consorcio"],
    "cvm_fii": ["real-estate-funds"], "bcb_fii": ["real-estate-funds"],
}


def _discovery_industries(hint: str | None) -> list[str]:
    h = (hint or "").strip()
    src = h.split(" ", 1)[0]
    if src in _DISCOVERY_SOURCE_IND:
        return list(_DISCOVERY_SOURCE_IND[src])
    if src == "bcb":  # discover_bcb_institutions: "bcb <ClassLabel> cnpj=..." → class→industry
        try:
            from src.synth.entity_discovery import _BCB_CLASS_MAP
            klass = h[4:].rsplit(" cnpj=", 1)[0].strip()
            ind = (_BCB_CLASS_MAP.get(klass) or (None,))[0]
            return [ind] if ind else []
        except Exception:  # pragma: no cover
            return []
    return []


# --- CPO (product) — rich per-sector Product intelligence -----------------------------
_TIER_WEIGHT = {"official": 1.0, "structured": 0.75, "registry": 0.5, "identified": 0.25}
_PRODUCT_FIELDS = ("ownership", "ticker", "parent", "esg", "certifications")


def _days_between(a: str | None, b: str | None) -> int | None:
    from datetime import date
    try:
        ya, ma, da = (int(x) for x in str(a)[:10].split("-"))
        yb, mb, db = (int(x) for x in str(b)[:10].split("-"))
        return abs((date(ya, ma, da) - date(yb, mb, db)).days)
    except Exception:
        return None


def _provenance_mix(attrs: list[dict[str, Any]]) -> dict[str, int]:
    mix: dict[str, int] = {}
    for a in attrs:
        r = (a or {}).get("radar")
        tier = r.get("tier") if isinstance(r, dict) else None
        if tier:
            mix[tier] = mix.get(tier, 0) + 1
    return mix


def _provenance_score(mix: dict[str, int]) -> int:
    n = sum(mix.values())
    if not n:
        return 0
    return round(100 * sum(_TIER_WEIGHT.get(t, 0) * c for t, c in mix.items()) / n)


def _sector_profile(feed: dict[str, Any], meta: dict[str, Any], cards_by: dict[str, list],
                    ents_by: dict[str, list], disc_by: dict[str, int]) -> dict[str, Any]:
    """A deep, decision-grade per-sector Product profile derived from feed.industries +
    entity_attrs + the sector's cards (+ discovery pipeline). Composite scores are transparent,
    documented formulas surfaced as *inferences*."""
    slug = meta.get("slug")
    as_of = feed.get("as_of")
    cards = cards_by.get(slug, [])
    attrs = ents_by.get(slug, [])
    universe = meta.get("entities") or 0
    tracked = meta.get("active_entities") or 0
    narratives = meta.get("narratives") or 0
    latest_n = meta.get("narratives_latest") or 0
    alerts = meta.get("alerts") or 0

    # per-entity card concentration (top-3 share) + source diversity + freshness
    per_ent: dict[str, int] = {}
    lenses: set[str] = set()
    dates: list[str] = []
    for c in cards:
        if c.get("entity"):
            per_ent[c["entity"]] = per_ent.get(c["entity"], 0) + 1
        lenses.update(c.get("lenses") or [])
        if c.get("date"):
            dates.append(str(c["date"]))
    n_cards = len(cards)
    top3 = sorted(per_ent.values(), reverse=True)[:3]
    concentration = round(sum(top3) / n_cards, 2) if n_cards else 0.0
    freshness_days = _days_between(as_of, max(dates)) if dates else None

    mix = _provenance_mix(attrs)
    prov_score = _provenance_score(mix)
    # per-sector field completeness (% of the sector's entities carrying each Product field)
    n_attrs = len(attrs) or 1
    completeness = {f: round(100 * sum(1 for a in attrs if (a or {}).get(f)) / n_attrs)
                    for f in _PRODUCT_FIELDS}

    # Coverage-maturity index (0–100, inference): breadth×depth×freshness×provenance.
    tracked_norm = min(tracked / 20, 1.0)
    vol_norm = min(narratives / 120, 1.0)
    fresh_norm = 1.0 if (freshness_days is not None and freshness_days <= 2) else (0.5 if latest_n else 0.15)
    maturity = round(100 * (0.30 * tracked_norm + 0.30 * vol_norm + 0.20 * fresh_norm + 0.20 * prov_score / 100))

    return {
        "slug": slug, "label": meta.get("display_name") or slug, "tier": meta.get("tier"),
        "industries": [slug],
        "covered": bool(meta.get("covered")), "coverage_gap": bool(meta.get("coverage_gap")),
        "low_volume": bool(meta.get("low_volume")),
        "universe": universe, "tracked": tracked, "narratives": narratives,
        "narratives_latest": latest_n, "alerts": alerts,
        "alert_rate": round(alerts / narratives, 2) if narratives else 0.0,
        "peak_score": meta.get("peak_score"),
        "cards": n_cards, "distinct_entities": len(per_ent), "lens_diversity": len(lenses),
        "freshness_days": freshness_days, "concentration": concentration,
        "discovery": disc_by.get(slug, 0),
        "provenance": mix, "provenance_score": prov_score, "completeness": completeness,
        "maturity": maturity,
    }


def _ifdata_market_labeled(feed: dict[str, Any]) -> dict[str, Any]:
    """#75: the IF.data system-wide size block with its top entities resolved to display labels."""
    m = dict(feed.get("ifdata_market") or {})
    if not m:
        return {}
    ea = feed.get("entity_attrs") or {}
    m["top"] = [{**t, "label": (ea.get(t.get("entity")) or {}).get("label") or t.get("entity")}
                for t in (m.get("top") or [])]
    return m


def build_cpo(feed: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    industries = list(feed.get("industries") or [])
    gaps = list(feed.get("coverage_gaps") or [])
    reviews = [r for r in (feed.get("reviews") or []) if r.get("kind") == "discovery"]
    rc = feed.get("regulatory_coverage") or {}

    # index cards + entities + discovery by sector (single pass each)
    cards_by: dict[str, list] = {}
    for c in ctx["cards"]:
        for s in (c.get("industries") or []):
            cards_by.setdefault(s, []).append(c)
    ents_by: dict[str, list] = {}
    for a in (feed.get("entity_attrs") or {}).values():
        for s in ((a or {}).get("industries") or []):
            ents_by.setdefault(s, []).append(a)
    disc_rows = [{"id": r.get("review_id"), "proposed": r.get("proposed"), "reason": r.get("reason"),
                  "hint": r.get("hint"), "confidence": r.get("confidence"),
                  "industries": _discovery_industries(r.get("hint"))}
                 for r in reviews]
    disc_by: dict[str, int] = {}
    for d in disc_rows:
        for s in d["industries"]:
            disc_by[s] = disc_by.get(s, 0) + 1

    portfolio = [_sector_profile(feed, i, cards_by, ents_by, disc_by) for i in industries]
    portfolio.sort(key=lambda p: p["maturity"], reverse=True)
    by_slug = {p["slug"]: p for p in portfolio}

    # top entities per sector (by card volume) — the sector's tracked competitive set
    top_entities: dict[str, list] = {}
    labels = ctx["labels"]
    for s, cards in cards_by.items():
        cnt: dict[str, int] = {}
        for c in cards:
            if c.get("entity"):
                cnt[c["entity"]] = cnt.get(c["entity"], 0) + 1
        top_entities[s] = [{"entity": e, "label": labels.get(e, e), "cards": n, "industries": [s]}
                           for e, n in sorted(cnt.items(), key=lambda kv: kv[1], reverse=True)[:12]]

    coverage_map = [{"slug": p["slug"], "label": p["label"], "covered": p["covered"],
                     "coverage_gap": p["coverage_gap"], "low_volume": p["low_volume"],
                     "narratives": p["narratives"], "active_entities": p["tracked"],
                     "maturity": p["maturity"], "industries": [p["slug"]]} for p in portfolio]
    blind = sorted(gaps, key=lambda g: (g.get("status") != "open", -(g.get("count") or 0)))
    blind_rows = [{"id": g.get("id"), "question": g.get("question"), "count": g.get("count"),
                   "status": g.get("status"), "reason": g.get("reason"),
                   "triage": (g.get("triage") or {}).get("class"),
                   "issue_url": g.get("issue_url"), "industries": []} for g in blind]

    # portfolio-level field-completeness (exposes the thin Product fields, e.g. esg/certifications)
    all_attrs = [a for a in (feed.get("entity_attrs") or {}).values()]
    n_all = len(all_attrs) or 1
    field_completeness = {f: round(100 * sum(1 for a in all_attrs if (a or {}).get(f)) / n_all)
                          for f in _PRODUCT_FIELDS}

    # ADR 022 (CPO angle): prudential-soundness INSTRUMENTATION coverage per sector — how much of
    # each sector's tracked portfolio actually carries Basileia (Tier A) and a real-text tone
    # (pilar3). A Product/coverage-maturity signal, NOT the CRO's competitor-risk read.
    solv_cov: dict[str, dict[str, int]] = {}
    for e in (feed.get("entities") or []):
        has_s = (e.get("soundness") or {}).get("indice_basileia") is not None
        has_p3 = (e.get("financial_tone") or {}).get("corpus") == "pilar3"
        for s in (e.get("industries") or []):
            c = solv_cov.setdefault(s, {"tracked": 0, "with_soundness": 0, "with_pilar3": 0})
            c["tracked"] += 1
            c["with_soundness"] += int(has_s)
            c["with_pilar3"] += int(has_p3)

    def _cov_pct(s):
        c = solv_cov.get(s)
        return round(100 * c["with_soundness"] / c["tracked"]) if c and c["tracked"] else None

    # Only sectors where prudential soundness APPLIES. A non-prudential sector (betting/funds/VC has
    # no Índice de Basileia) shows 0% — or a misleading low % from a single outlier institution
    # mis-clustered into it (e.g. one bank arm tagged into agri-funds). We ingest the whole IF.data
    # universe, so applicability = a CLUSTER of regulated institutions (≥2 with Basileia); anything
    # less is excluded rather than shown as a false coverage "gap".
    _PRUDENTIAL_MIN = 2
    soundness_coverage = sorted(
        [{"slug": s, "label": (by_slug.get(s) or {}).get("label", s),
          "tracked": c["tracked"], "with_soundness": c["with_soundness"],
          "with_pilar3": c["with_pilar3"], "coverage_pct": _cov_pct(s),
          "industries": [s]} for s, c in solv_cov.items() if c["with_soundness"] >= _PRUDENTIAL_MIN],
        key=lambda r: (r["coverage_pct"] if r["coverage_pct"] is not None else 999))

    def agg(slug):
        if slug == ALL:
            tot = sum(c["tracked"] for c in solv_cov.values())
            hav = sum(c["with_soundness"] for c in solv_cov.values())
            return {"n_covered": sum(1 for p in portfolio if p["covered"]),
                    "n_coverage_gap": sum(1 for p in portfolio if p["coverage_gap"]),
                    "n_reviews": len(disc_rows),
                    "n_gaps": sum(1 for g in blind_rows if g["status"] == "open"),
                    "maturity": round(sum(p["maturity"] for p in portfolio) / len(portfolio)) if portfolio else 0,
                    "provenance_score": _provenance_score(_provenance_mix(all_attrs)),
                    "soundness_coverage_pct": round(100 * hav / tot) if tot else None}
        p = by_slug.get(slug) or {}
        return {"n_covered": 1 if p.get("covered") else 0,
                "n_coverage_gap": 1 if p.get("coverage_gap") else 0,
                "n_reviews": p.get("discovery", 0),
                "n_gaps": sum(1 for g in blind_rows if g["status"] == "open"),
                "maturity": p.get("maturity", 0), "provenance_score": p.get("provenance_score", 0),
                "narratives": p.get("narratives", 0), "tracked": p.get("tracked", 0),
                "freshness_days": p.get("freshness_days"), "concentration": p.get("concentration"),
                "soundness_coverage_pct": _cov_pct(slug)}

    recs = []
    # weakest covered sector by maturity → deepen coverage
    weak = sorted([p for p in portfolio if p["covered"]], key=lambda p: p["maturity"])[:1]
    if weak:
        w = weak[0]
        recs.append(_rec("30d", f"Aprofundar cobertura em {w['label']} (maturidade {w['maturity']})",
                         "propose_vertical", officer="cpo", industries=[w["slug"]]))
    # thinnest Product field across the base → an ingestion requirement
    thin = sorted(field_completeness.items(), key=lambda kv: kv[1])[:1]
    if thin:
        f, pctv = thin[0]
        recs.append(_rec("estrategico", f"Fechar lacuna de metadado '{f}' (só {pctv}% da base)",
                         "propose_vertical", officer="cpo"))
    open_gaps = [g for g in blind_rows if g["status"] == "open"]
    if open_gaps:
        recs.append(_rec("imediato", f"Triagem de ponto cego: {open_gaps[0]['question']}",
                         "resolve_review", officer="cpo"))
    # Pilar 3 tone coverage is the UNCONFOUNDED instrumentation gap: among institutions that DO carry
    # Basileia (prudential), how many have their real risk-report (Pilar 3) ingested for tone. Unlike
    # a Basileia-coverage %, this can't be inflated by non-prudential entities (funds/betting/pure
    # insurers have no Basileia and aren't in the denominator) — so it's the honest Product ask.
    n_prudential = sum(r["with_soundness"] for r in soundness_coverage)
    n_pilar3 = sum(r["with_pilar3"] for r in soundness_coverage)
    if n_prudential and n_pilar3 < n_prudential:
        recs.append(_rec("30d",
                         f"Ampliar cobertura de tom Pilar 3 — só {n_pilar3}/{n_prudential} instituições "
                         f"prudenciais com relatório de risco ingerido",
                         "propose_vertical", officer="cpo"))

    return {"by_industry": _by_industry(ctx["sectors"], agg), "panels": {
        "portfolio": portfolio,
        "coverage_map": coverage_map,
        "top_entities": top_entities,
        "blind_spots": blind_rows[:30],
        "discovery": disc_rows[:40],
        "field_completeness": field_completeness,
        # SURF-4: growth/portfolio frameworks (Ansoff vectors + BCG quadrant) routed to the CPO.
        "ansoff": _framework_rows(feed, "ansoff")[:20],
        "bcg": _framework_rows(feed, "bcg")[:20],
        # SURF-5: product-move feed — launches/offers (ofertas/produto lens) for the CPO.
        "product_moves": [_headline(c) for c in sorted(
            (c for c in ctx["cards"] if set(c.get("lenses") or []) & {"ofertas", "produto"}),
            key=lambda c: str(c.get("date") or ""), reverse=True)[:24]],
        "soundness_coverage": soundness_coverage,               # ADR 022 (CPO instrumentation angle)
        "source_health": feed.get("source_health") or [],       # R5 (lens-freshness proxy)
        "source_runs": feed.get("source_runs") or [],            # #76 (real per-ingester reliability)
        "market_structure": feed.get("market_structure") or {},  # R3 (CVM revenue, listed issuers)
        "ifdata_market": _ifdata_market_labeled(feed),           # #75 (IF.data system-wide asset base)
        "pricing": feed.get("pricing") or {},                    # R4
        "reg_coverage": {"summary": rc.get("summary") or {},
                         "entity_covered": rc.get("entity_covered") or [],
                         "signal_only": rc.get("signal_only") or [], "gap": rc.get("gap") or []},
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


# --- Reference / playbooks (§H) — curated baseline per officer, labelled *referência* -
REFERENCE: dict[str, Any] = {
    "cso": {"title": "Playbook estratégico", "sections": [
        {"h": "Frameworks de estratégia (8)", "items": [
            "SWOT — forças, fraquezas, oportunidades, ameaças",
            "TOWS — cruzamento SO / ST / WO / WT", "Cinco Forças de Porter", "PESTLE",
            "Ansoff — matriz produto × mercado", "BCG — crescimento × participação",
            "Quatro Cantos (Four Corners)", "McKinsey 7S"]},
        {"h": "Eixos de posição", "items": [
            "Ameaça (0–100) × Expansão — o mapa de posição competitiva",
            "Momentum na janela (média recente − anterior)", "Grupos econômicos (feed.groups)"]}]},
    "cro": {"title": "Referência regulatória", "sections": [
        {"h": "Ciclo do ato normativo", "items": [
            "Consulta pública → norma → fiscalização", "Blast-radius (alcance) × dificuldade",
            "Prazos de vigência e de adequação"]},
        {"h": "Reguladores e instrumentos (BR-FS)", "items": [
            "CVM — Resolução / Instrução / Deliberação",
            "BCB / CMN — Resolução BCB/CMN, Circular, Instrução Normativa",
            "SUSEP / CNSP — Circular / Resolução", "PREVIC — previdência complementar fechada"]}]},
    "cco": {"title": "Playbook de compliance", "sections": [
        {"h": "Taxonomia de risco", "items": [
            "Distress societário (recuperação judicial / falência)", "Sanções — CEIS / CNEP",
            "Antitruste — CADE", "Reputação-como-risco — reclamações (BCB / Reclame Aqui)",
            "Integridade do registro (anomalias / atribuição)"]},
        {"h": "Modelo de governança (ADR-018)", "items": [
            "Proveniência por campo (inferido < enrich < descoberta < estruturado < curado < fixture)",
            "Precedência de escrita — automação não rebaixa o curado",
            "Auditoria contínua + rollback sobre o journal"]},
        {"h": "Guardrails", "items": [
            "LGPD — pessoas só como figuras públicas em papel público",
            "Difamação — não atribuir insolvência à contraparte citada numa notícia",
            "attribution_role — observadores (B3 / Serasa / reguladores) não são sujeitos"]}]},
    "cpo": {"title": "Referência de produto & cobertura", "sections": [
        {"h": "Cobertura", "items": [
            "Mapa CVM / BCB — segmentos regulados (roster / sinal / lacuna)",
            "Radar de proveniência por tier (official / structured / registry / identified)",
            "Pontos cegos — perguntas sem resposta (loop de cobertura)"]},
        {"h": "Descoberta & fontes", "items": [
            "Propostas de descoberta (review-gated)", "Registro de fontes por vertical (ADR-019)",
            "JTBD — quais tarefas do comprador a base atende"]}]},
}


def build_reference() -> dict[str, Any]:
    return REFERENCE


# --- top level ------------------------------------------------------------------------
def build_executive(feed: dict[str, Any], *, decisions: list[dict[str, Any]] | None = None,
                    engagement: list[dict[str, Any]] | None = None,
                    tdr_baseline_hours: float | None = None,
                    outcome_review_days: int = 7) -> dict[str, Any]:
    """`feed.executive` — the four enriched officer blocks + shared sectors + the Executive Flow
    (§D trajectories) + the Decision-Trust metrics (§E, from `decisions`) + the Executive
    Engagement rollup (§E, from `engagement`)."""
    cards = _cards(feed)
    dates = list(feed.get("dates") or [])
    recent, prior = _recent_window(dates)
    sectors = [{"slug": o.get("slug"), "label": o.get("display_name") or o.get("label") or o.get("slug")}
               for o in (feed.get("industry_options") or []) if o.get("slug")]
    labels = _labels(feed)
    ctx = {"cards": cards, "dates": dates, "recent": recent, "prior": prior, "labels": labels,
           "sectors": sectors, "reg_cards": [c for c in cards if c.get("kind") in _REG_KINDS]}
    engagement_roll = {}
    metrics = {}
    try:
        from src.synth import decision_metrics, engagement_log
        engagement_roll = engagement_log.aggregate(engagement or [], labels=labels)
        metrics = decision_metrics.compute_metrics(decisions or [], engagement=engagement_roll,
                                                   tdr_baseline_hours=tdr_baseline_hours,
                                                   outcome_review_days=outcome_review_days)
    except Exception as exc:  # pragma: no cover - metrics best-effort
        print(f"Warning: decision metrics skipped: {exc}")
    # DEC-3: auto-draft — each Executive-Flow trajectory is a pre-filled decision draft. Annotate
    # it with whether a decision already exists (context_id == trajectory.id) so the officer sees
    # an explicit pending worklist (undecided) vs the resolved ones, lifting capture.
    flow = build_flow(feed, ctx)
    _by_ctx = {d.get("context_id"): d for d in (decisions or []) if d.get("context_id")}
    n_drafts = 0
    for t in flow:
        d = _by_ctx.get(t.get("id"))
        t["decided"] = bool(d)
        t["verdict"] = d.get("verdict") if d else None
        t["decision_id"] = d.get("decision_id") if d else None
        if not d:
            n_drafts += 1
    if isinstance(metrics, dict):
        metrics["n_drafts"] = n_drafts
    return {
        "officers": list(OFFICERS),
        "generated_at": feed.get("generated_at"),
        "as_of": feed.get("as_of"),
        "sectors": sectors,
        "flow": flow,
        "metrics": metrics,
        "engagement": engagement_roll,
        "reference": REFERENCE,
        # SURF-8: cross-officer quiet-alert — expected-but-absent signals (silence axis), inference.
        "silence": sorted((feed.get("silence") or []),
                          key=lambda s: (s.get("silence_tier") or 0, s.get("score") or 0), reverse=True)[:20],
        "cso": build_cso(feed, ctx),
        "cro": build_cro(feed, ctx),
        "cco": build_cco(feed, ctx),
        "cpo": build_cpo(feed, ctx),
    }
