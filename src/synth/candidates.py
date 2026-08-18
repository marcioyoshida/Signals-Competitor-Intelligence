"""Extract fusion candidates from an ingest digest (Stage B quality bar).

Uses both delta `items` (is_new) and `context` samples so real digests still
yield product narratives after state seeding empties the delta lists.

Quality gates (env-overridable via kwargs from the handler):
  - Prefer multi-lens entity clusters (≥ min_lenses, default 2)
  - Single-lens only if is_new and lens is high-value
  - Drop market-only / weak context noise
  - Optional PL floor on fund AUM context
  - Attach digest as_of dates onto candidates
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Any
from uuid import uuid4

from src.synth.entities import resolve_entities, signal_blob, tokens_for_match

# Lens priority when ranking multi-signal entity clusters.
LENS_WEIGHT = {
    "regulatory": 0.35,
    "fatos": 0.3,
    "dou": 0.3,
    "sec": 0.25,
    "ofertas": 0.2,
    "inf_diario": 0.15,
    "pix": 0.12,
    "juros": 0.12,
    "entrants": 0.18,
    "funds": 0.15,
    "news": 0.12,
    "market": 0.08,
}

# Single-lens candidates allowed only for these lenses when is_new.
# Lenses whose lone NEW signal is strong enough to surface/alert on its own.
# News is intentionally excluded — it's "color" (low strategic weight); a single
# headline must be corroborated by another lens to surface, else a routine promo
# becomes a false alert.
HIGH_VALUE_SOLO_LENSES = frozenset(
    {"regulatory", "fatos", "dou", "sec", "ofertas", "entrants", "funds"}
)

# Market is backdrop only — never a solo seed.
BACKDROP_LENSES = frozenset({"market"})


def extract_candidates(
    digest: dict[str, Any],
    max_candidates: int = 10,
    *,
    min_lenses: int | None = None,
    min_pl: float | None = None,
    min_score: float | None = None,
    require_change: bool | None = None,
) -> list[dict[str, Any]]:
    """Build correlation candidates from digest sections + context.

    With ``require_change`` (default on), only candidates carrying a genuine
    delta (``is_new`` — set on new documents and threshold-exceeding numeric
    moves) are emitted. This is the "report change, not presence" rule: without
    it, a multi-lens entity built purely from steady-state context surfaces a
    near-duplicate narrative every day, polluting the feed.
    """
    min_lenses = (
        min_lenses
        if min_lenses is not None
        else int(os.environ.get("ONCA_SYNTH_MIN_LENSES", "2"))
    )
    require_change = (
        require_change
        if require_change is not None
        else os.environ.get("ONCA_SYNTH_CHANGE_ONLY", "true").lower()
        in ("1", "true", "yes")
    )
    min_pl = (
        min_pl
        if min_pl is not None
        else float(os.environ.get("ONCA_SYNTH_MIN_FUND_PL", "100000000"))  # R$100M
    )
    min_score = (
        min_score
        if min_score is not None
        else float(os.environ.get("ONCA_SYNTH_MIN_SCORE", "0.40"))
    )

    as_of = _digest_as_of(digest)
    signals = _collect_signals(digest, min_pl=min_pl)
    if not signals:
        return []

    # 1) Entity clusters (multi-lens fusion) — primary product unit
    clusters = _cluster_by_entity(signals, min_lenses=min_lenses)
    candidates: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for entity_id, members in clusters.items():
        if len(candidates) >= max_candidates * 2:  # gather then gate/sort
            break
        cand = _candidate_from_cluster(entity_id, members, as_of=as_of)
        for s in cand["sources"]:
            if s.get("id"):
                used_ids.add(str(s["id"]))
        candidates.append(cand)

    # 2) Regulatory seeds not already clustered (topic fusion)
    for reg in signals:
        if len(candidates) >= max_candidates * 2:
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
        threat, factors = _score_threat(sources)
        candidates.append(
            {
                "id": f"cand-{_short_id(rid or uuid4().hex)}",
                "kind": "regulatory_fusion" if related else "regulatory",
                "seed": reg,
                "related": related,
                "sources": sources,
                "entities": resolve_entities(reg),
                "lenses": _lenses(sources),
                "threat_score": threat,
                "threat_factors": factors,
                "is_alert": bool(reg.get("is_new") or any(r.get("is_new") for r in related)),
                "as_of": as_of,
                "data_as_of": _sources_as_of(sources, as_of),
            }
        )

    # 3) High-value NEW competitor alerts only (no weak context solo seeds)
    for sig in sorted(signals, key=_signal_rank, reverse=True):
        if len(candidates) >= max_candidates * 2:
            break
        sid = str(sig.get("id") or "")
        if sid and sid in used_ids:
            continue
        if not sig.get("is_new"):
            continue
        if sig.get("_lens") in BACKDROP_LENSES:
            continue
        if sid:
            used_ids.add(sid)
        related = _soft_related(sig, signals, used_ids, limit=3)
        for r in related:
            if r.get("id"):
                used_ids.add(str(r["id"]))
        sources = [sig, *related]
        threat, factors = _score_threat(sources)
        candidates.append(
            {
                "id": f"cand-{_short_id(sid or uuid4().hex)}",
                "kind": f"competitor:{sig.get('_lens') or 'signal'}",
                "seed": sig,
                "related": related,
                "sources": sources,
                "entities": resolve_entities(sig),
                "lenses": _lenses(sources),
                "threat_score": threat,
                "threat_factors": factors,
                "is_alert": True,
                "as_of": as_of,
                "data_as_of": _sources_as_of(sources, as_of),
            }
        )

    # Quality gate (+ emit-on-change: drop steady-state restatements)
    candidates = [
        c
        for c in candidates
        if _passes_quality_gate(c, min_lenses=min_lenses, min_score=min_score)
        and (not require_change or _has_change(c))
    ]

    candidates.sort(
        key=lambda c: (
            1 if c.get("is_alert") else 0,
            len(c.get("lenses") or []),
            c.get("threat_score") or 0,
        ),
        reverse=True,
    )
    return candidates[:max_candidates]


def _has_change(cand: dict[str, Any]) -> bool:
    """True if any source is a genuine delta (new doc or threshold move)."""
    return any(s.get("is_new") for s in (cand.get("sources") or []))


def _passes_quality_gate(
    cand: dict[str, Any],
    *,
    min_lenses: int,
    min_score: float,
) -> bool:
    lenses = [x for x in (cand.get("lenses") or []) if x not in BACKDROP_LENSES]
    # Counting backdrop separately: multi-lens can include market as 3rd
    all_lenses = cand.get("lenses") or []
    n_core = len(lenses)
    n_all = len(all_lenses)
    score = float(cand.get("threat_score") or 0)
    is_alert = bool(cand.get("is_alert"))
    seed_lens = (cand.get("seed") or {}).get("_lens")

    # Always drop pure market-only
    if n_all == 1 and seed_lens in BACKDROP_LENSES:
        return False
    if n_core == 0 and n_all <= 1:
        return False

    # Prefer multi-lens product unit
    if n_all >= min_lenses and score >= min_score * 0.85:
        return True
    if n_core >= min_lenses and score >= min_score * 0.85:
        return True

    # Solo only for new high-value alerts
    if is_alert and n_core == 1 and seed_lens in HIGH_VALUE_SOLO_LENSES and score >= min_score:
        return True
    if is_alert and n_all >= 2 and score >= min_score * 0.9:
        return True

    return False


def _digest_as_of(digest: dict[str, Any]) -> str | None:
    """Best-effort digest timestamp for product surfaces."""
    inf = digest.get("inf_diario_moves") or {}
    if isinstance(inf, dict) and inf.get("as_of"):
        return str(inf["as_of"])[:10]
    # fall back to newest filed/event_date in any section
    dates: list[str] = []
    for key in ("sec_filings", "ofertas", "regulatory", "competitor"):
        for item in _section_pool(digest, key):
            for dk in ("filed", "event_date", "registered", "date"):
                v = item.get(dk)
                if v:
                    dates.append(str(v)[:10])
    return max(dates) if dates else None


def _sources_as_of(sources: list[dict[str, Any]], fallback: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in sources:
        lens = str(s.get("_lens") or "signal")
        for dk in ("filed", "event_date", "registered", "date", "anomes"):
            if s.get(dk):
                out[lens] = str(s[dk])[:10]
                break
    if fallback and "digest" not in out:
        out["digest"] = fallback
    return out


def _collect_signals(
    digest: dict[str, Any],
    *,
    min_pl: float = 0.0,
) -> list[dict[str, Any]]:
    """Flatten items + context from each section into tagged signal dicts."""
    sections = [
        ("regulatory", "regulatory"),
        ("competitor", "funds"),
        ("new_entrants", "entrants"),
        ("ofertas", "ofertas"),
        ("fatos", "fatos"),
        ("dou", "dou"),
        ("news", "news"),
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
            # Noise: funds below the AUM floor. Use absolute PL so near-zero or
            # negative-AUM shells (which yield meaningless |pct_change| > 100%,
            # e.g. a R$-34k fund reading "-104.51%") are dropped even when the
            # move is_new — otherwise they headline the feed as false threats.
            if lens == "inf_diario":
                pl = row.get("pl")
                try:
                    if pl is not None and abs(float(pl)) < min_pl:
                        continue
                except (TypeError, ValueError):
                    pass
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
    *,
    min_lenses: int = 2,
) -> dict[str, list[dict[str, Any]]]:
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sig in signals:
        for ent in resolve_entities(sig):
            clusters[ent].append(sig)
    kept: dict[str, list[dict[str, Any]]] = {}
    for ent, members in clusters.items():
        uniq: dict[str, dict[str, Any]] = {}
        for m in members:
            uniq[str(m.get("id"))] = m
        members = list(uniq.values())
        lenses = {m.get("_lens") for m in members}
        core = lenses - BACKDROP_LENSES
        has_new = any(m.get("is_new") for m in members)
        has_move = any(
            m.get("pct_change") is not None and abs(float(m.get("pct_change") or 0)) >= 10
            for m in members
        )
        # Product bar: multi-lens preferred; single-lens only for new high-value
        if len(lenses) >= min_lenses or len(core) >= min_lenses:
            kept[ent] = members
        elif has_new and (core & HIGH_VALUE_SOLO_LENSES):
            kept[ent] = members
        elif has_move and len(core) >= 1 and len(lenses) >= 2:
            kept[ent] = members
    return kept


def _candidate_from_cluster(
    entity_id: str,
    members: list[dict[str, Any]],
    *,
    as_of: str | None = None,
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
    threat, factors = _score_threat(sources)
    return {
        "id": f"cand-ent-{entity_id}",
        "kind": "entity_fusion",
        "entity": entity_id,
        "seed": seed,
        "related": related,
        "sources": sources,
        "entities": [entity_id],
        "lenses": _lenses(sources),
        "threat_score": threat,
        "threat_factors": factors,
        "is_alert": any(m.get("is_new") for m in members),
        "as_of": as_of,
        "data_as_of": _sources_as_of(sources, as_of),
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


# How strategic each lens is on its own (0–1). Drives the score's "signal" axis:
# a regulatory change or a new entrant matters more than a routine fund filing.
STRATEGIC_WEIGHT = {
    "regulatory": 1.0,
    "entrants": 0.9,
    "fatos": 0.88,
    "dou": 0.82,
    "sec": 0.85,
    "ofertas": 0.6,
    "pix": 0.55,
    "juros": 0.55,
    "inf_diario": 0.5,
    "funds": 0.4,
    "news": 0.55,  # qualitative color — lower authority than official filings
    "market": 0.3,
}

# For a new entrant, the license class refines the signal: a new credit fintech
# (SCD/SEP) or payment institution is a sharper competitive signal than a new
# consórcio or cooperative. Keyed on bcb_autorizacoes.classify_license labels.
ENTRANT_CLASS_WEIGHT = {
    "Crédito Direto (SCD)": 0.95,
    "Empréstimo P2P (SEP)": 0.95,
    "Instituição de Pagamento": 0.9,
    "Banco": 0.9,
    "Financeira (SCFI)": 0.8,
    "Microcrédito (SCMEPP)": 0.7,
    "Corretora/DTVM": 0.5,
    "Companhia Hipotecária": 0.45,
    "Leasing": 0.45,
    "Agência de Fomento": 0.4,
    "Cooperativa": 0.25,
    "Consórcio": 0.2,
}


def _source_signal(s: dict[str, Any]) -> float:
    """Strategic weight of one source; entrants refined by license class."""
    lens = s.get("_lens")
    if lens == "entrants":
        return ENTRANT_CLASS_WEIGHT.get(s.get("license_class"), 0.6)
    return STRATEGIC_WEIGHT.get(str(lens), 0.4)


def _score_weights() -> dict[str, float]:
    """Blend weights for the four score axes (env-tunable), normalized to sum 1."""
    w = {
        "signal": float(os.environ.get("ONCA_SCORE_W_SIGNAL", "0.45")),
        "magnitude": float(os.environ.get("ONCA_SCORE_W_MAGNITUDE", "0.30")),
        "novelty": float(os.environ.get("ONCA_SCORE_W_NOVELTY", "0.15")),
        "breadth": float(os.environ.get("ONCA_SCORE_W_BREADTH", "0.10")),
    }
    total = sum(w.values()) or 1.0
    return {k: v / total for k, v in w.items()}


def _magnitude(sources: list[dict[str, Any]]) -> float:
    """Largest normalized numeric move among sources (0–1). 20%→0.5, 50%→0.71."""
    best = 0.0
    for s in sources:
        pct = s.get("pct_change")
        if pct is None:
            continue
        try:
            p = abs(float(pct))
        except (TypeError, ValueError):
            continue
        best = max(best, p / (p + 20.0))
    return best


def _score_threat(sources: list[dict[str, Any]]) -> tuple[float, dict[str, float]]:
    """Estimated threat score (0–1) as a transparent weighted blend of four axes.

    Not a calibrated model — a bounded, explainable estimate that spreads across
    the range (a weighted average of 0–1 axes only reaches 1.0 if every axis
    does). Returns (score, factor breakdown) so the UI can show *why*.

    - signal:    most strategic lens present (regulatory/entrant > routine filing)
    - magnitude: biggest normalized numeric move (pct_change)
    - novelty:   genuine delta (is_new) vs steady-state context
    - breadth:   multi-lens corroboration, with diminishing returns
    """
    lenses = _lenses(sources)
    core = [l for l in lenses if l not in BACKDROP_LENSES]
    signal = max((_source_signal(s) for s in sources), default=0.3)
    magnitude = _magnitude(sources)
    novelty = 1.0 if any(s.get("is_new") for s in sources) else 0.3
    n = len(core) or (1 if lenses else 0)
    breadth = 1.0 - 0.6 ** n if n else 0.0

    w = _score_weights()
    score = (
        w["signal"] * signal
        + w["magnitude"] * magnitude
        + w["novelty"] * novelty
        + w["breadth"] * breadth
    )
    factors = {
        "signal": round(signal, 3),
        "magnitude": round(magnitude, 3),
        "novelty": round(novelty, 3),
        "breadth": round(breadth, 3),
    }
    return round(min(1.0, score), 3), factors


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
