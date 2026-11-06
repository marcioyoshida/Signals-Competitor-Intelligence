"""Entity lifecycle gate (#364) — a liquidated / bankrupt / intervened institution is not a
competitor with growth options.

Found 2026-11-06 (Will Bank audit): the live feed showed Will Bank (in liquidação extrajudicial
since 2026-01-21) "expanding… integrating Mastercard" at 0.9 confidence (Ansoff — the source was
Mastercard settling its card bills in liquidation) and Banco Master (liquidated 2025-11-18) with
SWOT opportunities in bets / crypto / expansion, a "Lançamento de produto" behavioral front, and
an "Autorização BCB" badge inferred from the industry tag.

The gate runs at feed build, over the TRUSTED own distress rows already in ``feed.distress``
(regulator-sourced, curated or corroborated — a single-outlet report never gates an entity):

  - ``entity_attrs[e]["lifecycle"]`` = {status, kind, since, label, source, act, gated};
  - growth content is withheld for a gated entity: SWOT Strengths/Opportunities (+ proposals),
    TOWS, Ansoff, BCG, Four Corners, and the behavioral "pattern" cards;
  - the industry-inferred "Autorização BCB" certification is withdrawn (an institution under a
    BCB resolution regime no longer runs under its authorisation as a going concern).

RAET does not stop operations (Lei 9.447/1997) — it is labelled but not gated.
News cards, weaknesses, threats and enforcement rows stay: they are what is happening.
"""
from __future__ import annotations

from typing import Any

from src.synth.distress import TRUSTED_CONFIDENCE

STATUS_BY_KIND = {
    "falencia": ("falida", "Falida", True),
    "liquidacao_extrajudicial": ("em_liquidacao", "Em liquidação extrajudicial", True),
    "intervencao": ("sob_intervencao", "Sob intervenção do Banco Central", True),
    "raet": ("sob_raet", "Sob RAET (administração especial temporária)", False),
}
_ORDER = ("falencia", "liquidacao_extrajudicial", "intervencao", "raet")
GROWTH_STORES = ("tows", "ansoff", "bcg", "four_corners")


def _closed(row: dict[str, Any]) -> bool:
    st = str(row.get("regime_status") or "").lower()
    return bool(st) and st != "ativo" and row.get("kind") != "falencia"


def lifecycle_map(distress_rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """entity_id → lifecycle, from the entity's OWN trusted resolution / bankruptcy rows."""
    best: dict[str, dict[str, Any]] = {}
    for r in distress_rows or []:
        eid, kind = r.get("entity"), r.get("kind")
        if (not eid or (r.get("role") or "self") != "self" or kind not in STATUS_BY_KIND
                or r.get("confidence") not in TRUSTED_CONFIDENCE or _closed(r)):
            continue
        cur = best.get(eid)
        if cur is None or _ORDER.index(kind) < _ORDER.index(cur["kind"]):
            status, label, gated = STATUS_BY_KIND[kind]
            best[eid] = {"status": status, "kind": kind, "label": label, "gated": gated,
                         "since": r.get("first_seen") or r.get("date"),
                         "source": r.get("latest_url"), "act": r.get("act"),
                         "confidence": r.get("confidence")}
    return best


def apply_gate(feed: dict[str, Any]) -> dict[str, Any]:
    """Stamp lifecycles and withhold growth content for gated entities, in place. Returns
    ``{entity_id: lifecycle}``."""
    life = lifecycle_map(feed.get("distress"))
    if not life:
        return life
    gated = {e for e, v in life.items() if v["gated"]}
    attrs = feed.setdefault("entity_attrs", {})
    for eid, v in life.items():
        a = attrs.setdefault(eid, {})
        a["lifecycle"] = v
        if v["gated"]:
            # the industry-inferred badge AND a register-stamped "… em funcionamento" (R2 stamps
            # are additive and outlive the institution: MW IP kept one after its 10-29 liquidation)
            a["certifications"] = [c for c in (a.get("certifications") or [])
                                   if not (isinstance(c, dict) and c.get("basis") == "industry")
                                   and "em funcionamento" not in str(c)]
    swot = feed.get("swot") or {}
    for eid in gated & set(swot):
        sb = swot[eid] or {}
        dims = sb.get("dimensions") or {}
        for d in ("S", "O"):
            if d in dims:
                dims[d] = []
        if isinstance(sb.get("counts"), dict):
            sb["counts"].update(S=0, O=0)
        sb["lifecycle"] = life[eid]["label"]
    for key in GROWTH_STORES:
        store = feed.get(key)
        if isinstance(store, dict):
            for eid in gated & set(store):
                store.pop(eid, None)
    if isinstance(feed.get("swot_proposals"), list):
        feed["swot_proposals"] = [p for p in feed["swot_proposals"]
                                  if not (p.get("entity") in gated
                                          and str(p.get("dimension") or p.get("dim") or "")[:1] in ("S", "O"))]
    if isinstance(feed.get("feed"), list):
        out = []
        for c in feed["feed"]:
            ent = c.get("entity")
            if ent in gated and str(c.get("id") or "").startswith("behavioral-"):
                continue  # "múltiplas frentes … Lançamento de produto" for a liquidated bank
            if ent in life:
                c["lifecycle"] = life[ent]["label"]
            out.append(c)
        feed["feed"] = out
    print(f"Lifecycle gate: {len(life)} entities ({len(gated)} gated): "
          + ", ".join(f"{e}={v['status']}" for e, v in sorted(life.items())))
    return life
