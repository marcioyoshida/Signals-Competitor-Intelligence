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


def compute_metrics(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up the decision log into the honest, available Decision-Trust metrics + per-officer /
    per-industry slices. `ets`/`tdr` are intentionally None (inputs not yet captured)."""
    decisions = [d for d in (decisions or []) if isinstance(d, dict)]
    overall = _slice(decisions)

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
        "ets": None,
        "ets_note": "parcial — só o componente Feedback (resultado favorável) é medível hoje; "
                    "Influência/Engajamento/Adoção exigem a telemetria do beacon (Passo 5).",
        "tdr": None,
        "tdr_note": "requer baseline de tempo-para-decisão por tenant (registrado, não assumido).",
        "by_officer": by_officer,
        "by_industry": by_industry,
    }
