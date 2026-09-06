"""ADR 021 §E — Decision-Trust metrics from the OncaDecisionLog.

**Metrics honesty (the guardrail).** The full ETS = 0.40·Feedback + 0.25·Decision-Influence +
0.20·Engagement + 0.15·Board-Adoption. Today the log carries decisions + realized outcomes but
NOT the engagement / board-adoption telemetry (that arrives with the §H beacon, Step 5). So we
compute ONLY the measurable pieces and never fabricate the rest:

- **Feedback component** — the favorable-outcome rate among *resolved* decisions (0–10). This is
  the one ETS input we can measure now; surfaced as `ets_feedback` (labelled *parcial*).
- **Decision-Influence rate** — share of captured decisions that reached an observed outcome.
- **Approval rate**, **outcome mix**, per-officer and per-industry rollups.

`ets` (the composite) stays **None** until its inputs exist; `tdr` stays **None** because
Time-to-Decision Reduction needs a per-tenant *baseline* that must be recorded, never assumed.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

_RESOLVED = {"favoravel", "desfavoravel", "neutro"}


def _slice(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(decisions)
    approved = sum(1 for d in decisions if d.get("verdict") == "aprovado")
    resolved = [d for d in decisions if d.get("outcome") in _RESOLVED]
    favorable = sum(1 for d in resolved if d.get("outcome") == "favoravel")
    mix = Counter(d.get("outcome") or "pendente" for d in decisions)
    fav_rate = (favorable / len(resolved)) if resolved else 0.0
    # Board-adoption: measurable once ANY decision has been board-flagged (True/False set).
    flagged = [d for d in decisions if d.get("board_at") or d.get("board_adopted") is not None]
    board_adopted = sum(1 for d in flagged if d.get("board_adopted"))
    return {
        "n_decisions": n,
        "n_approved": approved,
        "approval_rate": round(approved / n, 3) if n else 0.0,
        "n_resolved": len(resolved),
        "influence_rate": round(len(resolved) / n, 3) if n else 0.0,
        "favorable_rate": round(fav_rate, 3),
        "outcome_mix": {k: mix.get(k, 0) for k in ("favoravel", "desfavoravel", "neutro", "pendente")},
        "n_board_flagged": len(flagged),
        "board_rate": round(board_adopted / len(flagged), 3) if flagged else 0.0,
        # measurable ETS input only — 0–10 feedback component; composite deferred (honest).
        "ets_feedback": round(fav_rate * 10, 1) if resolved else None,
    }


def _hours_between(start: str | None, end: str | None) -> float | None:
    """Elapsed hours between two ISO timestamps, or None if unparseable / non-positive."""
    import datetime as _dt
    try:
        a = _dt.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = _dt.datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        h = (b - a).total_seconds() / 3600.0
        return round(h, 2) if h >= 0 else None
    except Exception:
        return None


def outcomes_due(decisions: list[dict[str, Any]], *, min_age_days: int = 7,
                 now: str | None = None, cap: int = 30) -> list[dict[str, Any]]:
    """The **outcome-review queue** (ADR-021 §E, DEC-1): captured decisions whose outcome is still
    ``pendente`` and that are old enough (``min_age_days``) to have an observable result. This is
    what DRIVES :func:`decision_log.set_outcome` — without it, outcomes are only ever stamped in
    the same session as capture, and the favorable-rate reward signal never accrues. Oldest first
    (most overdue), capped. Pure projection over the passed decisions — no store access."""
    import datetime as _dt
    try:
        ref = (_dt.datetime.fromisoformat(str(now).replace("Z", "+00:00")) if now
               else _dt.datetime.now(_dt.timezone.utc))
    except Exception:
        ref = _dt.datetime.now(_dt.timezone.utc)
    out: list[dict[str, Any]] = []
    for d in decisions:
        if (d.get("outcome") or "pendente") != "pendente":
            continue
        age_h = _hours_between(d.get("created_at"), ref.isoformat())
        if age_h is None or age_h / 24.0 < min_age_days:
            continue
        out.append({
            "decision_id": d.get("decision_id"),
            "officer": (d.get("officer") or "").lower() or None,
            "industry": d.get("industry"),
            "recommendation": d.get("recommendation"),
            "verdict": d.get("verdict"),
            "created_at": d.get("created_at"),
            "age_days": int(age_h / 24.0),
            "context_id": d.get("context_id"),
            "evidence_id": d.get("evidence_id"),
        })
    out.sort(key=lambda x: str(x.get("created_at") or ""))  # oldest (most overdue) first
    return out[:cap]


def compute_metrics(decisions: list[dict[str, Any]],
                    engagement: dict[str, Any] | None = None,
                    tdr_baseline_hours: float | None = None,
                    outcome_review_days: int = 7) -> dict[str, Any]:
    """Roll up the decision log into the honest, available Decision-Trust metrics + per-officer /
    per-industry slices, folding the §E **Engagement** component from the engagement rollup.

    ETS = 0.40·Feedback + 0.25·Influence + 0.20·Engagement + 0.15·Board. Feedback/Influence/
    Engagement are now measurable; **Board adoption is not**, so `ets` is a PARTIAL composite —
    the weighted average of the measured components, renormalized to their summed weight (0.85),
    on a 0–10 scale, clearly labelled. `tdr` stays None (needs a per-tenant baseline)."""
    decisions = [d for d in (decisions or []) if isinstance(d, dict)]
    overall = _slice(decisions)

    # component scores (0–10); None when not yet measurable
    feedback = overall.get("ets_feedback")                        # 0.40
    influence = round(overall["influence_rate"] * 10, 1) if overall["n_decisions"] else None  # 0.25
    n_interest = (engagement or {}).get("n_interest") or 0
    engagement_score = round(min(n_interest / 50.0, 1.0) * 10, 1) if n_interest else None      # 0.20
    board = round(overall["board_rate"] * 10, 1) if overall["n_board_flagged"] else None       # 0.15

    comps = [(0.40, feedback), (0.25, influence), (0.20, engagement_score), (0.15, board)]
    measured = [(w, v) for w, v in comps if v is not None]
    ets = round(sum(w * v for w, v in measured) / sum(w for w, _ in measured), 1) if measured else None
    full = len(measured) == 4  # all four components measured ⇒ the FULL composite

    by_officer: dict[str, Any] = {}
    for off in ("cso", "cro", "cco", "cpo"):
        ds = [d for d in decisions if (d.get("officer") or "").lower() == off]
        if ds:
            by_officer[off] = _slice(ds)

    by_industry: dict[str, Any] = {}
    for ind in {d.get("industry") for d in decisions if d.get("industry")}:
        by_industry[ind] = _slice([d for d in decisions if d.get("industry") == ind])

    return {
        **overall,
        "ets": ets,
        "ets_full": full,
        "ets_components": {"feedback": feedback, "influence": influence,
                           "engagement": engagement_score, "board": board},
        "ets_note": ("ETS composto (0–10): Feedback 0.40 · Influência 0.25 · Engajamento 0.20 · "
                     "Adoção do board 0.15."
                     if full else
                     "ETS parcial (0–10) — média ponderada dos componentes medidos (Feedback 0.40 · "
                     "Influência 0.25 · Engajamento 0.20 · Adoção do board 0.15), renormalizada aos "
                     "que já têm sinal."),
        **_tdr(decisions, tdr_baseline_hours),
        "outcomes_due": outcomes_due(decisions, min_age_days=outcome_review_days),
        "outcome_review_days": outcome_review_days,
        "by_officer": by_officer,
        "by_industry": by_industry,
    }


def _tdr(decisions: list[dict[str, Any]], baseline_hours: float | None) -> dict[str, Any]:
    """Time-to-Decision Reduction = (Before − After)/Before × 100. `After` is the executive's
    real deliberation time (first-look `started_at` → `created_at`, measured client-side);
    `Before` is the RECORDED per-tenant baseline (never assumed). None until both exist."""
    afters = [h for h in (_hours_between(d.get("started_at"), d.get("created_at"))
                          for d in decisions if d.get("started_at")) if h is not None]
    after_avg = round(sum(afters) / len(afters), 2) if afters else None
    if not baseline_hours:
        return {"tdr": None, "tdr_after_hours": after_avg, "tdr_baseline_hours": None,
                "tdr_note": "requer baseline de tempo-para-decisão por tenant (registrado, "
                            "não assumido) — defina ONCA_TDR_BASELINE_HOURS."}
    if after_avg is None:
        return {"tdr": None, "tdr_after_hours": None, "tdr_baseline_hours": baseline_hours,
                "tdr_note": f"baseline {baseline_hours}h registrada; medindo o tempo real de "
                            "deliberação (nenhuma decisão temporizada ainda)."}
    tdr = round((baseline_hours - after_avg) / baseline_hours * 100, 1)
    return {"tdr": tdr, "tdr_after_hours": after_avg, "tdr_baseline_hours": baseline_hours,
            "tdr_note": f"Before {baseline_hours}h (baseline registrada) → After {after_avg}h "
                        f"(deliberação medida, n={len(afters)})."}
