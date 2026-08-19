"""Wave 1 (ADR 003) — the Longitudinal axis: X broke its own pattern.

The second consumer of the Wave 0 feature store. Where silence narrates an
*absence*, longitudinal narrates a **trajectory break**: an entity whose most
recent activity sits anomalously far from its own historical threat baseline —
`score_z` (latest peak vs the mean/σ of everything before it) beyond a
threshold, in either direction:

- **escalation** (z ≥ +Z) — recent threat well above the entity's own norm,
- **cooling**    (z ≤ −Z) — a sharp de-escalation below its norm.

Design (per ADR 003):
- **Deterministic nominator, no LLM.** The gate is arithmetic on the feature
  store; the card is a heuristic pt-BR briefing.
- **Grounded inference.** Unlike silence (no source), a break HAS real driving
  signals — the card cites the driving narrative as evidence (Decision 4) while
  staying labeled inference (`is_inference` / `mode="derived"` / `axis="longitudinal"`).
- **Lag-free.** A break matters the day it happens, so this recomputes features
  from the freshest history (it runs after synth) rather than reading the
  pre-synth `features/latest.json` snapshot.
- **No feedback loop.** `longitudinal` is a derived axis (excluded from
  `feature_store` activity), so a break card never re-shapes the baseline.
- **No overlap with silence.** Gated on recency (`days_since_last ≤ RECENCY_DAYS`);
  silence gates on absence — mutually exclusive.
- **Break-tuned emit-on-change.** A standing break is suppressed within a cooldown
  unless it escalates a tier; a break that normalizes is retracted same-day.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

from src.synth import feature_store
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

AXIS = "longitudinal"

# --- Nomination gate (env-overridable) -------------------------------------
# Need a real baseline: >= this many active days => >= 3 points before the latest.
MIN_ACTIVE_DAYS = 4
# |score_z| beyond this is a break (z already floors its denom at MIN_STD).
Z_THRESHOLD = 2.0
# ... and the absolute move must be material, so a tight baseline can't turn a
# trivial wobble into a big z.
MIN_ABS_MOVE = 0.12
# The break must be CURRENT (latest active day within this many days of now).
RECENCY_DAYS = 3
# Re-emit suppression: within this many days, only escalation re-fires.
COOLDOWN_DAYS = 5


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return float(default)


def break_tier(abs_z: float) -> int:
    """Escalation tier from |score_z|."""
    if abs_z >= 4.5:
        return 3
    if abs_z >= 3.0:
        return 2
    return 1


def _days_between(a: str, b: str) -> int | None:
    try:
        return (feature_store._parse(a) - feature_store._parse(b)).days
    except Exception:
        return None


def drivers(narratives: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per entity, the ACTIVITY narrative that drove its latest active day.

    The highest-threat narrative on the entity's most recent active date — the
    evidence a break card cites. Derived/inference cards are excluded.
    """
    best: dict[str, dict[str, Any]] = {}
    for n in narratives:
        if not feature_store.is_activity_narrative(n):
            continue
        ent = n.get("entity")
        date = feature_store._date_of(n)
        if not ent or not date:
            continue
        cur = best.get(ent)
        score = _score(n.get("threat_score"))
        if (
            cur is None
            or date > cur["_date"]
            or (date == cur["_date"] and score > cur["_score"])
        ):
            best[ent] = {"_date": date, "_score": score, "narrative": n}
    return best


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def prior_breaks(narratives: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Most-recent prior break card per entity: {entity: {run_date, tier, direction}}."""
    out: dict[str, dict[str, Any]] = {}
    for n in narratives:
        if not isinstance(n, dict) or n.get("axis") != AXIS:
            continue
        ent = n.get("entity")
        rd = feature_store._date_of(n)
        if not ent or not rd:
            continue
        prev = out.get(ent)
        if prev is None or rd > prev["run_date"]:
            out[ent] = {
                "run_date": rd,
                "tier": int(n.get("break_tier") or 1),
                "direction": n.get("direction"),
            }
    return out


def nominate(
    features: dict[str, Any],
    *,
    recent_narratives: list[dict[str, Any]] | None = None,
    as_of: str | None = None,
    cooldown_days: int | None = None,
) -> list[dict[str, Any]]:
    """Pure gate: fresh features -> trajectory-break candidates (no I/O)."""
    recent_narratives = recent_narratives or []
    as_of = as_of or features.get("as_of") or run_date_today()
    cooldown_days = int(
        cooldown_days
        if cooldown_days is not None
        else _f("ONCA_LONGITUDINAL_COOLDOWN_DAYS", COOLDOWN_DAYS)
    )
    min_active = int(_f("ONCA_LONGITUDINAL_MIN_ACTIVE_DAYS", MIN_ACTIVE_DAYS))
    z_thr = _f("ONCA_LONGITUDINAL_Z", Z_THRESHOLD)
    min_move = _f("ONCA_LONGITUDINAL_MIN_MOVE", MIN_ABS_MOVE)
    recency = _f("ONCA_LONGITUDINAL_RECENCY_DAYS", RECENCY_DAYS)

    driver_map = drivers(recent_narratives)
    prior = prior_breaks(recent_narratives)

    out: list[dict[str, Any]] = []
    for e in features.get("entities") or []:
        ent = e.get("entity")
        z = e.get("score_z")
        dsl = e.get("days_since_last")
        if not ent or z is None or dsl is None:
            continue
        if (e.get("active_days") or 0) < min_active or dsl > recency:
            continue

        score_last = float(e.get("score_last") or 0.0)
        score_mean = float(e.get("score_mean") or 0.0)
        if abs(score_last - score_mean) < min_move or abs(z) < z_thr:
            continue

        direction = "escalation" if z > 0 else "cooling"
        tier = break_tier(abs(z))

        # Break emit-on-change: skip a recent same-direction break unless escalated.
        prev = prior.get(ent)
        if prev is not None and prev.get("direction") == direction:
            gap = _days_between(as_of, prev["run_date"])
            if gap is not None and gap < cooldown_days and tier <= prev["tier"]:
                continue

        out.append(
            {
                "entity": ent,
                "label": e.get("label") or ENTITY_LABELS.get(ent, str(ent).title()),
                "direction": direction,
                "score_z": round(float(z), 2),
                "score_last": round(score_last, 3),
                "score_mean": round(score_mean, 3),
                "score_std": round(float(e.get("score_std") or 0.0), 3),
                "active_days": int(e.get("active_days") or 0),
                "last_seen": e.get("last_seen"),
                "industries": list(e.get("industries") or []),
                "break_tier": tier,
                "driver": driver_map.get(ent, {}).get("narrative"),
            }
        )
    # Sharpest escalations first, then sharpest coolings.
    out.sort(
        key=lambda c: (c["direction"] == "escalation", c["break_tier"], abs(c["score_z"])),
        reverse=True,
    )
    return out


def _break_score(cand: dict[str, Any]) -> float:
    """Escalation reports the real current level; cooling is a low watch signal."""
    if cand["direction"] == "escalation":
        return round(min(1.0, cand["score_last"]), 3)
    return round(min(0.35, 0.15 + cand["score_last"]), 3)


def build_narrative(cand: dict[str, Any]) -> dict[str, Any]:
    """Heuristic pt-BR break card — labeled inference, cites the driving signal."""
    label = cand["label"]
    z = cand["score_z"]
    last = cand["score_last"]
    mean = cand["score_mean"]
    score = _break_score(cand)
    driver = cand.get("driver") or {}
    citations = [c for c in (driver.get("citations") or []) if isinstance(c, dict)]
    source_ids = list(driver.get("source_ids") or [])

    if cand["direction"] == "escalation":
        head = (
            f"Quebra de padrão (alta): a ameaça de {label} saltou para {last:.2f}, "
            f"muito acima da sua própria média de {mean:.2f} (z={z:+.1f}, "
            f"{cand['active_days']} dias ativos)."
        )
    else:
        head = (
            f"Quebra de padrão (recuo): a ameaça de {label} caiu para {last:.2f}, "
            f"bem abaixo da sua própria média de {mean:.2f} (z={z:+.1f}, "
            f"{cand['active_days']} dias ativos)."
        )
    driver_txt = (driver.get("narrative") or "").strip()
    if driver_txt:
        head += f" Sinal que puxou a mudança: {driver_txt[:280]}"
    tail = (
        " Índice derivado do histórico do próprio concorrente (inferência de "
        "trajetória, não um fato novo) — os sinais citados são a evidência."
    )

    return {
        "id": f"longitudinal-{cand['entity']}",
        "kind": "longitudinal",
        "axis": AXIS,
        "subject_type": "entity",
        "direction": cand["direction"],
        "entity": cand["entity"],
        "entities": [cand["entity"]],
        "lenses": list(driver.get("lenses") or []),
        "is_alert": False,
        "is_inference": True,
        "threat_score": score,
        "threat_factors": {
            "score_z": z,
            "direction": cand["direction"],
            "score_last": last,
            "baseline_mean": mean,
            "break_tier": cand["break_tier"],
        },
        "threat_score_note": "estimated_v1_longitudinal",
        "break_tier": cand["break_tier"],
        "score_z": z,
        "last_seen": cand["last_seen"],
        "narrative": head + tail,
        "citations": citations,
        "source_ids": source_ids,
        "mode": "derived",
        "run_date": run_date_today(),
        "run_at": run_at_now(),
        "as_of": run_date_today(),
        "data_as_of": {"driver": (driver.get("as_of") or cand["last_seen"])},
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Recompute fresh features from history, emit trajectory-break narratives."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    window_days = int(_f("ONCA_LONGITUDINAL_WINDOW_DAYS", _f("ONCA_FEATURE_WINDOW_DAYS", 90)))
    s3 = boto3.client("s3")
    run_date = run_date_today()

    recent = feature_store.load_history(digests_bucket, window_days, s3=s3)
    # Fresh, lag-free features (build_features drops derived cards internally).
    features = feature_store.build_features(
        recent, as_of=run_date, industry_map=feature_store.load_industry_map()
    )
    if not features.get("entities"):
        return {"statusCode": 200, "body": json.dumps({"status": "no_features"})}

    cands = nominate(features, recent_narratives=recent, as_of=run_date)

    keys: list[str] = []
    breaking = {c["entity"] for c in cands}
    for cand in cands:
        key = _write(build_narrative(cand), digests_bucket, s3)
        if key:
            keys.append(key)

    # Same-day retraction: an entity that is active today but NO LONGER breaking
    # (recomputed |z| back within threshold) had any break card written earlier
    # today invalidated — retract it. Derived cards are recomputable; prior days
    # stay as history. (Only entities recent enough to have been nominated today.)
    z_thr = _f("ONCA_LONGITUDINAL_Z", Z_THRESHOLD)
    recency = _f("ONCA_LONGITUDINAL_RECENCY_DAYS", RECENCY_DAYS)
    normalized = {
        e["entity"]
        for e in features.get("entities") or []
        if e.get("entity") not in breaking
        and e.get("score_z") is not None
        and (e.get("days_since_last") or 99) <= recency
        and abs(float(e["score_z"])) < z_thr
    }
    retracted = _retract_same_day(digests_bucket, s3, run_date, normalized)

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "status": "ok",
                "as_of": run_date,
                "window_days": window_days,
                "nominated": len(cands),
                "emitted": len(keys),
                "entities": [f"{c['entity']}:{c['direction']}" for c in cands],
                "keys": keys,
                "retracted": sorted(retracted),
            }
        ),
    }


def _retract_same_day(bucket: str, s3: Any, run_date: str, normalized: set[str]) -> list[str]:
    """Delete same-day break cards for entities that normalized today."""
    out: list[str] = []
    for ent in sorted(normalized):
        key = f"{feature_store.NARRATIVES_PREFIX}{run_date}/longitudinal-{ent}.json"
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except Exception:
            continue  # no same-day break card — nothing to retract
        try:
            s3.delete_object(Bucket=bucket, Key=key)
            out.append(ent)
        except Exception as exc:  # pragma: no cover - best-effort
            print(f"Warning: retract break card {key} failed: {exc}")
    return out


def _write(narrative: dict[str, Any], bucket: str, s3: Any) -> str | None:
    date = str(narrative.get("run_date") or narrative.get("as_of") or "unknown")[:10]
    key = f"{feature_store.NARRATIVES_PREFIX}{date}/{narrative['id']}.json"
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(narrative, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        return key
    except Exception as exc:  # pragma: no cover - write is best-effort
        print(f"Warning: write break narrative failed: {exc}")
        return None
