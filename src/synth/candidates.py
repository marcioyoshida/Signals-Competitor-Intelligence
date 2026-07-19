"""Extract fusion candidates from an ingest digest (Stage B hardened).

Uses both delta `items` (is_new) and `context` samples so real digests still
yield product narratives after state seeding empties the delta lists.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any
from uuid import uuid4

from src.synth.entities import primary_entity, resolve_entities, signal_blob, tokens_for_match

# Lens priority when ranking multi-signal entity clusters.
LENS_WEIGHT = {
    "regulatory": 0.35,
    "sec": 0.25,
    "ofertas": 0.2,
    "inf_diario": 0.15,
    "pix": 0.12,
    "juros": 0.12,
    "entrants": 0.18,
    "funds": 0.15,
    "market": 0.08,
}


def extract_candidates(
    digest: dict[str, Any],
    max_candidates: int = 10,
) -> list[dict[str, Any]]:
    """Build correlation candidates from digest sections + context."""
    signals = _collect_signals(digest)
    if not signals:
        return []

    # 1) Entity clusters (multi-lens fusion)
    clusters = _cluster_by_entity(signals)
    candidates: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for entity_id, members in clusters.items():
        if len(candidates) >= max_candidates:
            break
        cand = _candidate_from_cluster(entity_id, members)
        for s in cand["sources"]:
            if s.get("id"):
                used_ids.add(str(s["id"]))
        candidates.append(cand)

    # 2) Regulatory seeds not already in a cluster (topic fusion via soft match)
    for reg in signals:
        if len(candidates) >= max_candidates:
            break
        if reg.get("_lens") != "regulatory":
            continue
        rid = str(reg.get("id") or "")
        if rid and rid in used_ids:
            continue
        related = _soft_related(reg, signals, used_ids, limit=4)
        for r in related:
            if r.get("id"):
                used_ids.add(str(r["id"]))
        if rid:
            used_ids.add(rid)
        sources = [reg, *related]
        candidates.append(
            {
                "id": f"cand-{_short_id(rid or uuid4().hex)}",
                "kind": "regulatory_fusion" if related else "regulatory",
                "seed": reg,
                "related": related,
                "sources": sources,
                "entities": resolve_entities(reg),
                "lenses": _lenses(sources),
                "threat_score": _threat_score(sources),
                "is_alert": bool(reg.get("is_new") or any(r.get("is_new") for r in related)),
            }
        )

    # 3) Remaining high-value competitor alerts (new moves / filings)
    for sig in sorted(signals, key=_signal_rank, reverse=True):
        if len(candidates) >= max_candidates:
            break
        sid = str(sig.get("id") or "")
        if sid and sid in used_ids:
            continue
        if not sig.get("is_new") and sig.get("_lens") not in ("sec", "ofertas", "inf_diario"):
            # Prefer alerts; allow context only for high-value lenses as seed
            if not (sig.get("pct_change") and abs(float(sig.get("pct_change") or 0)) >= 15):
                continue
        if sid:
            used_ids.add(sid)
        related = _soft_related(sig, signals, used_ids, limit=3)
        for r in related:
            if r.get("id"):
                used_ids.add(str(r["id"]))
        sources = [sig, *related]
        candidates.append(
            {
                "id": f"cand-{_short_id(sid or uuid4().hex)}",
                "kind": f"competitor:{sig.get('_lens') or 'signal'}",
                "seed": sig,
                "related": related,
                "sources": sources,
                "entities": resolve_entities(sig),
                "lenses": _lenses(sources),
                "threat_score": _threat_score(sources),
                "is_alert": bool(sig.get("is_new")),
            }
        )

    candidates.sort(
        key=lambda c: (
            1 if c.get("is_alert") else 0,
            len(c.get("lenses") or []),
            c.get("threat_score") or 0,
        ),
        reverse=True,
    )
    return candidates[:max_candidates]


def _collect_signals(digest: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten items + context from each section into tagged signal dicts."""
    sections = [
        ("regulatory", "regulatory"),
        ("competitor", "funds"),
        ("new_entrants", "entrants"),
        ("ofertas", "ofertas"),
        ("sec_filings", "sec"),
        ("pix_moves", "pix"),
        ("juros_moves", "juros"),
        ("inf_diario_moves", "inf_diario"),
        ("market", "market"),
    ]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for section_key, lens in sections:
        for item in _section_pool(digest, section_key):
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row["_lens"] = lens
            # Market rows lack ids — synthesize stable ones
            if not row.get("id"):
                if lens == "market":
                    inst = row.get("institution") or "unknown"
                    row["id"] = f"market:{inst}"
                    row["source"] = row.get("source") or "BCB-IFDATA"
                else:
                    row["id"] = f"{lens}:{uuid4().hex[:10]}"
            iid = str(row["id"])
            if iid in seen:
                # Prefer keeping is_new=True version
                continue
            seen.add(iid)
            out.append(row)
    return out


def _section_pool(digest: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Merge delta items with context samples (items first)."""
    section = digest.get(key)
    legacy_lists = {
        "regulatory": digest.get("new_normativos"),
        "competitor": digest.get("new_fund_filings"),
        "ofertas": digest.get("new_ofertas"),
        "sec_filings": digest.get("new_sec_filings"),
    }
    items: list[dict[str, Any]] = []
    if isinstance(section, dict):
        items.extend(list(section.get("items") or []))
        items.extend(list(section.get("context") or []))
    elif isinstance(section, list):
        items.extend(section)
    legacy = legacy_lists.get(key)
    if isinstance(legacy, list):
        items.extend(legacy)
    # Dedup by id while preserving order
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id") or "")
        if iid and iid in seen:
            continue
        if iid:
            seen.add(iid)
        out.append(it)
    return out


def _cluster_by_entity(
    signals: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sig in signals:
        for ent in resolve_entities(sig):
            clusters[ent].append(sig)
    # Keep clusters with 2+ distinct lenses, or 1 lens if alert/new, or strong move
    kept: dict[str, list[dict[str, Any]]] = {}
    for ent, members in clusters.items():
        lenses = {m.get("_lens") for m in members}
        has_new = any(m.get("is_new") for m in members)
        has_move = any(
            m.get("pct_change") is not None and abs(float(m.get("pct_change") or 0)) >= 10
            for m in members
        )
        if len(lenses) >= 2 or has_new or has_move or len(members) >= 2:
            # de-dupe members by id
            uniq: dict[str, dict[str, Any]] = {}
            for m in members:
                uniq[str(m.get("id"))] = m
            kept[ent] = list(uniq.values())
    return kept


def _candidate_from_cluster(
    entity_id: str, members: list[dict[str, Any]]
) -> dict[str, Any]:
    # Seed: prefer regulatory, else SEC, else highest threat component
    def seed_key(m: dict[str, Any]) -> tuple:
        return (
            1 if m.get("_lens") == "regulatory" else 0,
            1 if m.get("is_new") else 0,
            1 if m.get("_lens") == "sec" else 0,
            abs(float(m.get("pct_change") or 0)),
        )

    ordered = sorted(members, key=seed_key, reverse=True)
    seed = ordered[0]
    related = ordered[1:]
    sources = ordered
    return {
        "id": f"cand-ent-{entity_id}",
        "kind": "entity_fusion",
        "entity": entity_id,
        "seed": seed,
        "related": related,
        "sources": sources,
        "entities": [entity_id],
        "lenses": _lenses(sources),
        "threat_score": _threat_score(sources),
        "is_alert": any(m.get("is_new") for m in members),
    }


def _soft_related(
    seed: dict[str, Any],
    signals: list[dict[str, Any]],
    used: set[str],
    limit: int = 3,
) -> list[dict[str, Any]]:
    seed_ents = set(resolve_entities(seed))
    seed_toks = tokens_for_match(seed)
    related: list[dict[str, Any]] = []
    for sig in signals:
        sid = str(sig.get("id") or "")
        if sid and (sid == str(seed.get("id")) or sid in used):
            continue
        if sig.get("_lens") == seed.get("_lens") and not sig.get("is_new"):
            continue
        score = 0
        sig_ents = set(resolve_entities(sig))
        if seed_ents and seed_ents & sig_ents:
            score += 3
        overlap = seed_toks & tokens_for_match(sig)
        if len(overlap) >= 1:
            score += min(2, len(overlap))
        # Payments domain boost: pix regulatory + acquirer SEC
        blob = signal_blob(seed) + " " + signal_blob(sig)
        if "PIX" in blob and sig.get("_lens") in ("sec", "ofertas", "pix", "juros"):
            score += 1
        if score <= 0:
            continue
        related.append(sig)
        related.sort(
            key=lambda m: (
                1 if m.get("is_new") else 0,
                1 if set(resolve_entities(m)) & seed_ents else 0,
            ),
            reverse=True,
        )
        if len(related) >= limit:
            break
    return related[:limit]


def _lenses(sources: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for s in sources:
        lens = s.get("_lens")
        if lens and lens not in out:
            out.append(str(lens))
    return out


def _threat_score(sources: list[dict[str, Any]]) -> float:
    """Estimated multi-lens score — not a calibrated model."""
    score = 0.15
    lenses = _lenses(sources)
    for lens in lenses:
        score += LENS_WEIGHT.get(lens, 0.05)
    if any(s.get("is_new") for s in sources):
        score += 0.15
    for s in sources:
        pct = s.get("pct_change")
        if pct is not None:
            try:
                score += min(0.2, abs(float(pct)) / 100.0)
            except (TypeError, ValueError):
                pass
    # multi-lens bonus
    if len(lenses) >= 2:
        score += 0.1
    if len(lenses) >= 3:
        score += 0.1
    return round(min(1.0, score), 3)


def _signal_rank(sig: dict[str, Any]) -> float:
    base = LENS_WEIGHT.get(str(sig.get("_lens")), 0.05)
    if sig.get("is_new"):
        base += 0.5
    try:
        base += min(0.3, abs(float(sig.get("pct_change") or 0)) / 50.0)
    except (TypeError, ValueError):
        pass
    return base


def _short_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9:_-]+", "-", str(value))[:48]
    return cleaned or uuid4().hex[:12]
