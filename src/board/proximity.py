"""Competitor proximity (#385, ADR 031 amendment 2026-11-15): how close each competitor is to
the client's own (focal) institution, so a board brief weights competitor signals by distance.

Deterministic and explainable: code computes it, never the model. Each dimension gives a
similarity in [0, 1] (1 = alike), with the inputs it used and the pack fact ids they come
from. A missing input is reported as missing, never imputed. The total is the weighted mean
over the dimensions that could be computed, and ``coverage`` says how much weight that was;
below ``min_coverage`` the result is flagged ``low_coverage``.

Dimensions (weights in config/board_proximity.json, versioned):
- size            — total assets (IF.data), else market share: log-ratio distance;
- market          — overlap of licensed industries (Jaccard);
- business_model  — loan/funding ratio, credit cost, opex/assets, ROA (scaled abs. differences);
- prudential      — Basileia, leverage ratio, LCR, NPL (scaled abs. differences);
- pressure        — the competitor's recent threat score (0–100 → 0–1). Not a likeness: how
                    hard it is pushing. It is part of the total because a close, aggressive
                    competitor matters more than a close, quiet one.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "board_proximity.json"
DIMENSIONS = ("size", "market", "business_model", "prudential", "pressure")


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    return json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _slug(s: Any) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "x"


def _feature(e: Mapping[str, Any], block: str, key: str) -> float | None:
    return _num((e.get(block) or {}).get(key))


# feature -> (entity block, key, pack fact kind) — the fact kind matches src/board/pack.py ids
_BUSINESS = {"credito_captacoes_pct": ("fundamentals", "fundamentals"),
             "roa_pct": ("fundamentals", "fundamentals"),
             "custo_credito_pct": ("resultados", "results"),
             "opex_ativo_pct": ("resultados", "results")}
_PRUDENTIAL = {"indice_basileia": ("soundness", "soundness"),
               "razao_alavancagem": ("soundness", "soundness"),
               "lcr_pct": ("pilar3_km1", "km1"),
               "npl_total": ("inadimplencia", "npl")}


def _scaled(focal: Mapping[str, Any], peer: Mapping[str, Any], spec: Mapping[str, tuple[str, str]],
            scales: Mapping[str, float]) -> dict[str, Any] | None:
    """1 − mean(min(1, |a−b| / scale)) over the features both sides have."""
    diffs, inputs, facts = [], {}, set()
    for key, (block, kind) in spec.items():
        a, b = _feature(focal, block, key), _feature(peer, block, key)
        if a is None or b is None or not scales.get(key):
            continue
        diffs.append(min(1.0, abs(a - b) / float(scales[key])))
        inputs[key] = {"focal": a, "peer": b}
        facts.update({f"f-{kind}-{_slug(focal['entity'])}", f"f-{kind}-{_slug(peer['entity'])}"})
    if not diffs:
        return None
    return {"sim": round(1 - sum(diffs) / len(diffs), 3), "inputs": inputs, "facts": sorted(facts),
            "n_features": len(diffs), "of": len(spec)}


def _size(focal: Mapping[str, Any], peer: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict[str, Any] | None:
    zero = float((cfg.get("size") or {}).get("orders_of_magnitude_to_zero") or 2.0)
    for block, key, kind in (("fundamentals", "ativo_bi", "fundamentals"), (None, "market_share_pct", None)):
        a = _feature(focal, block, key) if block else _num(focal.get(key))
        b = _feature(peer, block, key) if block else _num(peer.get(key))
        if a and b and a > 0 and b > 0:
            orders = abs(math.log10(a / b))
            out = {"sim": round(max(0.0, 1 - orders / zero), 3), "inputs": {key: {"focal": a, "peer": b}},
                   "ratio": round(b / a, 3)}
            out["facts"] = ([f"f-{kind}-{_slug(focal['entity'])}", f"f-{kind}-{_slug(peer['entity'])}"]
                            if kind else [])
            return out
    return None


def _market(focal: Mapping[str, Any], peer: Mapping[str, Any]) -> dict[str, Any] | None:
    a, b = set(focal.get("industries") or []), set(peer.get("industries") or [])
    if not a or not b:
        return None
    return {"sim": round(len(a & b) / len(a | b), 3),
            "inputs": {"industries": {"focal": sorted(a), "peer": sorted(b), "shared": sorted(a & b)}},
            "facts": []}


def _pressure(peer_attrs: Mapping[str, Any] | None) -> dict[str, Any] | None:
    t = (peer_attrs or {}).get("threat_w3") or {}
    m = _num(t.get("mean"))
    if m is None:
        return None
    return {"sim": round(max(0.0, min(1.0, m / 100.0)), 3),
            "inputs": {"threat_mean": m, "window": [t.get("from"), t.get("to")], "n": t.get("n")},
            "facts": []}


def score(focal: Mapping[str, Any], peer: Mapping[str, Any], *, peer_attrs: Mapping[str, Any] | None = None,
          config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Proximity of ``peer`` to ``focal`` (both ``feed.entities`` rows)."""
    cfg = config or load_config()
    w = cfg["weights"]
    comps = {"size": _size(focal, peer, cfg),
             "market": _market(focal, peer),
             "business_model": _scaled(focal, peer, _BUSINESS, cfg.get("business_model") or {}),
             "prudential": _scaled(focal, peer, _PRUDENTIAL, cfg.get("prudential") or {}),
             "pressure": _pressure(peer_attrs)}
    have = {d: c for d, c in comps.items() if c is not None}
    total_w = sum(float(w.get(d, 0)) for d in DIMENSIONS)
    have_w = sum(float(w.get(d, 0)) for d in have)
    value = round(sum(float(w.get(d, 0)) * c["sim"] for d, c in have.items()) / have_w, 3) if have_w else None
    coverage = round(have_w / total_w, 3) if total_w else 0.0
    return {"peer": peer["entity"], "label": peer.get("label") or peer["entity"], "score": value,
            "coverage": coverage, "low_coverage": coverage < float(cfg.get("min_coverage") or 0.5),
            "components": have, "missing": [d for d in DIMENSIONS if d not in have],
            "config_version": cfg.get("version")}


def rank(feed: Mapping[str, Any], focal: str, *, candidates: list[str] | None = None, n: int | None = None,
         config: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every candidate's proximity to ``focal``, closest first. Candidates default to the
    entities sharing at least one industry with the focal institution. Only ids present in
    ``feed.entities`` are scored (ADR 022 duplicate-id trap)."""
    cfg = config or load_config()
    ents = {e["entity"]: e for e in feed.get("entities") or [] if e.get("entity")}
    if focal not in ents:
        raise ValueError(f"focal {focal!r} is not an entity in this feed")
    f = ents[focal]
    inds = set(f.get("industries") or [])
    pool = candidates if candidates is not None else [
        eid for eid, e in ents.items() if eid != focal and inds & set(e.get("industries") or [])]
    attrs = feed.get("entity_attrs") or {}
    out = [score(f, ents[c], peer_attrs=attrs.get(c), config=cfg) for c in pool if c in ents and c != focal]
    out.sort(key=lambda r: (r["low_coverage"], -(r["score"] if r["score"] is not None else -1), r["peer"]))
    return out[:n] if n else out


def fact(r: Mapping[str, Any], focal_label: str, as_of: Any = None) -> dict[str, Any]:
    """A pack fact for one proximity result (the shape src/board/pack.py emits), citable as
    ``f-proximity-<peer>``. Its text names every component and every missing dimension."""
    names = {"size": "porte", "market": "mercado", "business_model": "modelo de negócio",
             "prudential": "perfil prudencial", "pressure": "pressão competitiva"}
    parts = [f"{names[d]} {c['sim']:.2f}" for d, c in r["components"].items()]
    size = r["components"].get("size") or {}
    if size.get("ratio"):
        parts[0] += f" ({size['ratio']:.2f}× o porte de {focal_label})"
    text = (f"Proximidade de {r['label']} a {focal_label}: {r['score']:.2f} de 1 "
            f"(cobertura {r['coverage']:.0%}). Componentes: " + "; ".join(parts) + "."
            if r["score"] is not None else f"Proximidade de {r['label']} a {focal_label}: sem dados.")
    if r["missing"]:
        text += " Sem dados para: " + ", ".join(names[d] for d in r["missing"]) + "."
    if r["low_coverage"]:
        text += " Cobertura baixa: use com cautela."
    return {"id": f"f-proximity-{_slug(r['peer'])}", "kind": "proximity", "entity": r["peer"],
            "source": f"Onça (cálculo de proximidade {r.get('config_version')})", "as_of": as_of,
            "text": text, "data": {"score": r["score"], "coverage": r["coverage"],
                                   "components": {d: c["sim"] for d, c in r["components"].items()},
                                   "inputs_facts": sorted({x for c in r["components"].values() for x in c.get("facts", [])})}}
