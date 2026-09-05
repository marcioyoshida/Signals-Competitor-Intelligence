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
    return {
        "n_decisions": n,
        "n_approved": approved,
        "approval_rate": round(approved / n, 3) if n else 0.0,
        "n_resolved": len(resolved),
        "influence_rate": round(len(resolved) / n, 3) if n else 0.0,
        "favorable_rate": round(fav_rate, 3),
        "outcome_mix": {k: mix.get(k, 0) for k in ("favoravel", "desfavoravel", "neutro", "pendente")},
        # measurable ETS input only — 0–10 feedback component; composite deferred (honest).
        "ets_feedback": round(fav_rate * 10, 1) if resolved else None,
    }


def compute_metrics(decisions: list[dict[str, Any]],
                    engagement: dict[str, Any] | None = None) -> dict[str, Any]:
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

    comps = [(0.40, feedback), (0.25, influence), (0.20, engagement_score)]
    measured = [(w, v) for w, v in comps if v is not None]
    ets = round(sum(w * v for w, v in measured) / sum(w for w, _ in measured), 1) if measured else None

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
        "ets_components": {"feedback": feedback, "influence": influence,
                           "engagement": engagement_score, "board": None},
        "ets_note": "parcial (0–10) — média ponderada dos componentes medidos (Feedback 0.40 · "
                    "Influência 0.25 · Engajamento 0.20), renormalizada; Adoção do board (0.15) "
                    "ainda não instrumentada.",
        "tdr": None,
        "tdr_note": "requer baseline de tempo-para-decisão por tenant (registrado, não assumido).",
        "by_officer": by_officer,
        "by_industry": by_industry,
    }
