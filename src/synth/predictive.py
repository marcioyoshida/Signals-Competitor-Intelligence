"""Opportunistic (ADR 003) — Predictive / leading-indicator. TIME-GATED by design.

"What X is likely to do next." The ADR is explicit that this axis is **premature to
force**: it needs labelled pattern→outcome history and validation, and it "accrues as
history accumulates — treat it as a by-product of running the feature store for
months, not a scheduled build" (Suggested sequencing; Others to consider). Its risk
is **High (inference)**.

So this module ships the *mechanism* — deterministic precursor rules over the derived
layer (feature store + thread/behavioral signals) — behind an explicit **maturity
gate**. Until the feature store holds enough history AND the axis is enabled, it
emits nothing and reports `time_gated` with the current depth vs. the requirement.
When it does fire, a forecast is a **heavily labeled inference** (`is_inference`,
`mode="derived"`, `axis="predictive"`, note "estimativa, não previsão validada"),
grounded in the precursor signals it cites — never asserted as fact.

Precursor rules (deterministic, no ML/LLM in v1):
- **momentum_buildup** — an entity in longitudinal *escalation* whose activity cadence
  is tightening → likely to sustain/ą intensify near-term.
- **launch_precursor** — a recent *authorization* thread + *expansion* behavioral
  signature → likely product/market launch.

Both are precursors, not outcomes; the maturity gate is what separates "an interesting
pattern" from "a forecast we're willing to publish".
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

from src.synth import feature_store
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

AXIS = "predictive"

# Maturity gate: the feature store must span this many days of history, and the axis
# must be explicitly enabled, before any forecast is published (ADR: time-gated).
MIN_HISTORY_DAYS = 120
ENABLED_DEFAULT = "0"


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return float(default)


def _enabled() -> bool:
    return os.environ.get("ONCA_PREDICTIVE_ENABLED", ENABLED_DEFAULT) in ("1", "true", "True")


def _label(ent: str) -> str:
    return ENTITY_LABELS.get(ent) or str(ent).replace("_", " ").title()


def history_depth_days(narratives: list[dict[str, Any]]) -> int:
    """Span (days) between the earliest and latest ACTIVITY narrative — the maturity metric."""
    dates = sorted({feature_store._date_of(n) for n in narratives
                    if isinstance(n, dict) and feature_store.is_activity_narrative(n)
                    and feature_store._date_of(n)})
    if len(dates) < 2:
        return 0
    return (feature_store._parse(dates[-1]) - feature_store._parse(dates[0])).days


def leading_indicators(
    features: dict[str, Any], narratives: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Pure: derived layer -> precursor detections (no maturity gate applied here).

    The gate is enforced by the caller so tests can exercise the rules directly.
    """
    try:
        from src.synth import behavioral, threads
    except Exception:  # pragma: no cover
        behavioral = threads = None

    # entity -> recent event types + behavioral patterns (best-effort)
    ev_by_entity: dict[str, set[str]] = {}
    for n in narratives:
        if not isinstance(n, dict) or not feature_store.is_activity_narrative(n):
            continue
        ent = n.get("entity")
        if ent and threads is not None:
            ev_by_entity.setdefault(ent, set()).update(threads.event_types_of(n))

    patterns_by_entity: dict[str, set[str]] = {}
    if behavioral is not None:
        try:
            for c in behavioral.nominate(features, narratives):
                patterns_by_entity.setdefault(c["entity"], set()).add(c.get("pattern"))
        except Exception:  # pragma: no cover - behavioral optional
            patterns_by_entity = {}

    out: list[dict[str, Any]] = []
    z_hi = _f("ONCA_PREDICTIVE_ESCALATION_Z", 1.5)
    for e in features.get("entities") or []:
        ent = e.get("entity")
        if not ent:
            continue
        evs = ev_by_entity.get(ent, set())
        patterns = patterns_by_entity.get(ent, set())

        if float(e.get("score_z") or 0.0) >= z_hi and e.get("cadence_regular"):
            out.append({"entity": ent, "label": e.get("label") or _label(ent),
                        "signal": "momentum_buildup", "score_z": e.get("score_z"),
                        "horizon_days": 14})
        if "authorization" in evs and ("multi_front" in patterns or "expansion" in evs):
            out.append({"entity": ent, "label": e.get("label") or _label(ent),
                        "signal": "launch_precursor", "score_z": e.get("score_z"),
                        "horizon_days": 30})
    return out


_SIGNAL_PT = {
    "momentum_buildup": "momentum em construção — tende a sustentar/intensificar atividade",
    "launch_precursor": "precursor de lançamento — autorização recente + expansão",
}


def build_card(ind: dict[str, Any]) -> dict[str, Any]:
    label, sig = ind["label"], ind["signal"]
    head = (f"Indicador antecedente: {label} — {_SIGNAL_PT.get(sig, sig)} "
            f"(janela ~{ind['horizon_days']}d). Estimativa, NÃO previsão validada.")
    return {
        "id": f"predictive-{ind['entity']}-{sig}",
        "kind": "predictive", "axis": AXIS, "subject_type": "entity",
        "entity": ind["entity"], "entities": [ind["entity"]],
        "signal": sig, "pattern": sig, "horizon_days": ind["horizon_days"],
        "lenses": [], "is_alert": False, "is_inference": True,
        "threat_score": 0.2,
        "threat_factors": {"signal": sig, "score_z": ind.get("score_z")},
        "threat_score_note": "estimated_v1_predictive_unvalidated",
        "narrative": head, "citations": [], "source_ids": [],
        "mode": "derived",
        "run_date": run_date_today(), "run_at": run_at_now(), "as_of": run_date_today(),
        "data_as_of": {},
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Emit leading-indicator forecasts ONLY when history is mature and the axis is enabled."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    window = int(_f("ONCA_PREDICTIVE_WINDOW_DAYS", _f("ONCA_FEATURE_WINDOW_DAYS", 90)))
    s3 = boto3.client("s3")
    run_date = run_date_today()
    narratives = feature_store.load_history(digests_bucket, window, s3=s3)
    depth = history_depth_days(narratives)
    need = int(_f("ONCA_PREDICTIVE_MIN_HISTORY_DAYS", MIN_HISTORY_DAYS))

    if not _enabled() or depth < need:
        # ADR: time-gated — accrues as history accumulates. Honest no-op until then.
        return {"statusCode": 200, "body": json.dumps({
            "status": "time_gated", "as_of": run_date, "enabled": _enabled(),
            "history_depth_days": depth, "required_days": need, "run_at": run_at_now(),
        })}

    features = feature_store.build_features(
        narratives, as_of=run_date, industry_map=feature_store.load_industry_map())
    inds = leading_indicators(features, narratives)
    keys = []
    for ind in inds:
        key = f"{feature_store.NARRATIVES_PREFIX}{run_date}/{build_card(ind)['id']}.json"
        try:
            s3.put_object(Bucket=digests_bucket, Key=key,
                          Body=json.dumps(build_card(ind), ensure_ascii=False, indent=2).encode("utf-8"),
                          ContentType="application/json")
            keys.append(key)
        except Exception as exc:  # pragma: no cover
            print(f"Warning: write predictive card failed: {exc}")
    return {"statusCode": 200, "body": json.dumps({
        "status": "ok", "as_of": run_date, "history_depth_days": depth,
        "indicators": len(inds), "emitted": len(keys), "run_at": run_at_now(),
    })}
