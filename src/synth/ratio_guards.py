"""#300 / ADR 022 — per-metric sanity guards for derived prudential ratios.

Two ratios go implausible on atypical books, and an outlier there drives a false CSO "benchmark" or
CRO "fragility" rec:

- **ROA / ROE** (Tier 1, ``bcb_fundamentals``): an insurer appears through its bank arm, whose tiny
  IF.data asset base inflates ROA (SulAmérica 28%); micro-institutions swing to ±100%.
- **NPL** (Tier 2, ``bcb_inadimplencia``): a tiny or atypical credit book gives extreme ratios
  (SumUp 40.9% on R$ 60 mi), or an artifact where overdue equals the whole book (100%).

Two rules per metric, applied on READ (``fundamentals_by_entity`` / ``npl_by_entity``) so the
stored index keeps the raw values for audit and every consumer — feed, CSO/CRO panels, recs,
fragility, framework evidence — sees the same guarded value:

- **Denominator floor** → the metric is ``None`` with reason ``base_pequena``.
- **Plausibility band** → the metric is ``None`` with reason ``fora_da_faixa``.

The record carries ``guarded = {metric: reason}``. The suppressed value is NOT carried along: the
feed is agent grounding, and a raw outlier there would be quoted. Thresholds are
calibrated on the live 2026-03 IF.data base (124 fundamentals, 63 NPL records): a major bank's
values never trip them.
"""
from __future__ import annotations

from typing import Any

SMALL = "base_pequena"
OUT_OF_RANGE = "fora_da_faixa"

MIN_ATIVO_BI = 0.1          # R$ 100 mi of assets: below it, profit ratios are noise
ROA_BAND_PCT = 15.0         # |ROA| above 15% a.a. is not a bank's return on assets
ROE_BAND_PCT = 100.0        # |ROE| above 100% a.a.
MIN_CARTEIRA_BI = 0.1       # R$ 100 mi credit book
NPL_BAND_PCT = 50.0         # half the book 15+ days overdue: a data artifact, not a band


def _apply(rec: dict[str, Any], reasons: dict[str, str], *, also_null: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    if not reasons:
        return rec
    out = dict(rec)
    guarded = dict(out.get("guarded") or {})
    for metric, reason in reasons.items():
        for k in (metric, *also_null.get(metric, ())):
            out[k] = None
        guarded[metric] = reason
    out["guarded"] = guarded
    return out


def guard_fundamentals(rec: dict[str, Any]) -> dict[str, Any]:
    """ROA / ROE: floor on the asset base, then a plausibility band. Idempotent."""
    ativo = rec.get("ativo_bi")
    reasons: dict[str, str] = {}
    small = ativo is not None and ativo < MIN_ATIVO_BI
    for metric, band in (("roa_pct", ROA_BAND_PCT), ("roe_pct", ROE_BAND_PCT)):
        v = rec.get(metric)
        if v is None:
            continue
        if small:
            reasons[metric] = SMALL
        elif abs(v) > band:
            reasons[metric] = OUT_OF_RANGE
    return _apply(rec, reasons, also_null={})


def guard_npl(rec: dict[str, Any]) -> dict[str, Any]:
    """NPL: floor on the credit book, then a band on the total and on each PF/PJ segment.
    A guarded total also drops its band and its segments. Idempotent."""
    cart = rec.get("carteira_bi")
    total = rec.get("npl_total")
    reasons: dict[str, str] = {}
    if total is not None:
        if cart is not None and cart < MIN_CARTEIRA_BI:
            reasons["npl_total"] = SMALL
        elif total > NPL_BAND_PCT:
            reasons["npl_total"] = OUT_OF_RANGE
    if "npl_total" not in reasons:
        for seg in ("npl_pf", "npl_pj"):
            v = rec.get(seg)
            if v is not None and v > NPL_BAND_PCT:
                reasons[seg] = OUT_OF_RANGE
    return _apply(rec, reasons, also_null={"npl_total": ("band", "npl_pf", "npl_pj")})

