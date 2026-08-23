"""Wave 1 (ADR 003) — the Comparative / peer-cohort axis: X vs. its industry peers.

The third consumer of the Wave 0 feature store, and the **first structured feeder
into the SWOT belief store** (ADR 004). Where longitudinal compares an entity to its
*own* baseline and silence narrates an *absence*, comparative narrates a **peer
outlier**: an entity whose sustained threat level sits far from its industry cohort's:

- **outperform** (peer_z ≥ +Z) — runs hotter than its peers → a competitor
  **Strength** (a threat to our client),
- **underperform** (peer_z ≤ −Z) — runs cooler than its peers → a competitor
  **Weakness** (an opportunity for our client).

Design (per ADR 003, mirrors longitudinal):
- **Deterministic nominator, no LLM.** peer_z = (entity.score_mean − cohort.mean) /
  max(cohort.std, MIN_STD), computed on the fresh feature store's `cohorts` block.
- **Grounded inference.** Cites the entity's driving activity narrative as evidence
  (Decision 4) while labeled inference (`is_inference`/`mode="derived"`/`axis="comparative"`).
- **SWOT-mappable (ADR 004).** Each card carries a `swot_hint`
  (`dimension` S|W, `sign` +/−, `cohort`, `peer_z`) so the belief store consumes it
  verbatim — no LLM stance needed for this deterministic feeder.
- **No feedback loop.** `comparative` is a derived axis (excluded from feature_store
  activity), so a peer card never re-shapes any baseline.
- **Peer-tuned emit-on-change.** A standing outlier is suppressed within a cooldown
  unless it worsens a tier; an entity that returns to the pack is retracted same-day.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

from src.synth import feature_store, longitudinal
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

AXIS = "comparative"

# --- Nomination gate (env-overridable) -------------------------------------
MIN_PEERS = 3          # a cohort needs >= this many members to compare against
MIN_ACTIVE_DAYS = 3    # the entity needs enough of its own history to have a mean
PEER_Z = 1.5           # |peer_z| beyond this is an outlier
MIN_GAP = 0.1          # ... and the absolute gap vs the cohort mean must be material
RECENCY_DAYS = 7       # the entity must be reasonably current (not stale)
COOLDOWN_DAYS = 7      # re-emit suppression: only a worse tier re-fires within this


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return float(default)


def peer_tier(abs_z: float) -> int:
    if abs_z >= 3.0:
        return 3
    if abs_z >= 2.0:
        return 2
    return 1


def _industry_label(slug: str) -> str:
    try:
        from src.synth.entity_registry import INDUSTRIES

        return INDUSTRIES.get(slug, {}).get("display_name") or slug
    except Exception:  # pragma: no cover - registry import best-effort
        return slug


def prior_outliers(narratives: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Most-recent prior comparative card per entity: {entity: {run_date, tier, direction}}."""
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
                "tier": int(n.get("peer_tier") or 1),
                "direction": n.get("direction"),
            }
    return out


def _best_cohort(entity: dict[str, Any], cohorts: dict[str, Any], min_peers: int):
    """The industry cohort giving this entity its sharpest peer_z (|z|), or None."""
    best = None
    for slug in entity.get("industries") or []:
        c = cohorts.get(slug)
        if not c or int(c.get("members") or 0) < min_peers:
            continue
        std = max(float(c.get("score_std") or 0.0), feature_store.MIN_STD)
        z = (float(entity.get("score_mean") or 0.0) - float(c.get("score_mean") or 0.0)) / std
        if best is None or abs(z) > abs(best[1]):
            best = (slug, z, c)
    return best


def nominate(
    features: dict[str, Any],
    *,
    recent_narratives: list[dict[str, Any]] | None = None,
    as_of: str | None = None,
    cooldown_days: int | None = None,
) -> list[dict[str, Any]]:
    """Pure gate: fresh features -> peer-outlier candidates (no I/O)."""
    recent_narratives = recent_narratives or []
    as_of = as_of or features.get("as_of") or run_date_today()
    cooldown_days = int(
        cooldown_days if cooldown_days is not None
        else _f("ONCA_COMPARATIVE_COOLDOWN_DAYS", COOLDOWN_DAYS)
    )
    min_peers = int(_f("ONCA_COMPARATIVE_MIN_PEERS", MIN_PEERS))
    min_active = int(_f("ONCA_COMPARATIVE_MIN_ACTIVE_DAYS", MIN_ACTIVE_DAYS))
    z_thr = _f("ONCA_COMPARATIVE_Z", PEER_Z)
    min_gap = _f("ONCA_COMPARATIVE_MIN_GAP", MIN_GAP)
    recency = _f("ONCA_COMPARATIVE_RECENCY_DAYS", RECENCY_DAYS)

    cohorts = features.get("cohorts") or {}
    driver_map = longitudinal.drivers(recent_narratives)
    prior = prior_outliers(recent_narratives)

    out: list[dict[str, Any]] = []
    for e in features.get("entities") or []:
        ent = e.get("entity")
        dsl = e.get("days_since_last")
        if not ent or dsl is None or dsl > recency:
            continue
        if (e.get("active_days") or 0) < min_active:
            continue
        picked = _best_cohort(e, cohorts, min_peers)
        if picked is None:
            continue
        slug, z, c = picked
        gap = float(e.get("score_mean") or 0.0) - float(c.get("score_mean") or 0.0)
        if abs(gap) < min_gap or abs(z) < z_thr:
            continue

        direction = "outperform" if z > 0 else "underperform"
        tier = peer_tier(abs(z))

        prev = prior.get(ent)
        if prev is not None and prev.get("direction") == direction:
            g = longitudinal._days_between(as_of, prev["run_date"])
            if g is not None and g < cooldown_days and tier <= prev["tier"]:
                continue

        out.append(
            {
                "entity": ent,
                "label": e.get("label") or ENTITY_LABELS.get(ent, str(ent).title()),
                "direction": direction,
                "cohort": slug,
                "cohort_label": _industry_label(slug),
                "peers": int(c.get("members") or 0),
                "peer_z": round(float(z), 2),
                "entity_mean": round(float(e.get("score_mean") or 0.0), 3),
                "cohort_mean": round(float(c.get("score_mean") or 0.0), 3),
                "score_last": round(float(e.get("score_last") or 0.0), 3),
                "active_days": int(e.get("active_days") or 0),
                "last_seen": e.get("last_seen"),
                "peer_tier": tier,
                "driver": driver_map.get(ent, {}).get("narrative"),
            }
        )
    # Sharpest outperformers first (bigger competitive threat), then underperformers.
    out.sort(
        key=lambda c: (c["direction"] == "outperform", c["peer_tier"], abs(c["peer_z"])),
        reverse=True,
    )
    return out


def _peer_score(cand: dict[str, Any]) -> float:
    """Outperformance reports a real elevated level (capped context, not a top alert);
    underperformance is a low watch signal."""
    if cand["direction"] == "outperform":
        return round(min(0.55, cand["entity_mean"]), 3)
    return round(min(0.3, 0.12 + cand["entity_mean"]), 3)


def swot_hint(cand: dict[str, Any]) -> dict[str, Any]:
    """ADR 004: map the peer outlier onto a SWOT dimension for the belief store.
    Outperform => competitor Strength (+); underperform => competitor Weakness (−)."""
    return {
        "dimension": "S" if cand["direction"] == "outperform" else "W",
        "sign": "+" if cand["direction"] == "outperform" else "-",
        "cohort": cand["cohort"],
        "peer_z": cand["peer_z"],
    }


def build_narrative(cand: dict[str, Any]) -> dict[str, Any]:
    """Heuristic pt-BR peer-comparison card — labeled inference, cites the driver."""
    label = cand["label"]
    z = cand["peer_z"]
    em = cand["entity_mean"]
    cm = cand["cohort_mean"]
    coh = cand["cohort_label"]
    n = cand["peers"]
    score = _peer_score(cand)
    driver = cand.get("driver") or {}
    citations = [c for c in (driver.get("citations") or []) if isinstance(c, dict)]

    if cand["direction"] == "outperform":
        head = (
            f"Comparativo setorial: {label} opera ACIMA dos pares de {coh} — "
            f"ameaça média {em:.2f} vs {cm:.2f} do setor "
            f"(z setorial={z:+.1f}, {n} concorrentes). Sinal de força competitiva."
        )
    else:
        head = (
            f"Comparativo setorial: {label} opera ABAIXO dos pares de {coh} — "
            f"ameaça média {em:.2f} vs {cm:.2f} do setor "
            f"(z setorial={z:+.1f}, {n} concorrentes). Possível fraqueza relativa."
        )
    driver_txt = (driver.get("narrative") or "").strip()
    if driver_txt:
        head += f" Atividade recente: {driver_txt[:240]}"
    tail = (
        " Índice derivado da comparação com o cohort da indústria (inferência "
        "relativa, não um fato novo) — a atividade citada é a evidência."
    )

    return {
        "id": f"comparative-{cand['entity']}",
        "kind": "comparative",
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
            "peer_z": z,
            "direction": cand["direction"],
            "entity_mean": em,
            "cohort_mean": cm,
            "peers": n,
            "peer_tier": cand["peer_tier"],
        },
        "threat_score_note": "estimated_v1_comparative",
        "peer_tier": cand["peer_tier"],
        "peer_z": z,
        "cohort": cand["cohort"],
        "last_seen": cand["last_seen"],
        "swot_hint": swot_hint(cand),
        "narrative": head + tail,
        "citations": citations,
        "source_ids": list(driver.get("source_ids") or []),
        "mode": "derived",
        "run_date": run_date_today(),
        "run_at": run_at_now(),
        "as_of": run_date_today(),
        "data_as_of": {"driver": (driver.get("as_of") or cand["last_seen"])},
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Recompute fresh features, emit peer-cohort outlier narratives."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    window_days = int(_f("ONCA_COMPARATIVE_WINDOW_DAYS", _f("ONCA_FEATURE_WINDOW_DAYS", 90)))
    s3 = boto3.client("s3")
    run_date = run_date_today()

    recent = feature_store.load_history(digests_bucket, window_days, s3=s3)
    features = feature_store.build_features(
        recent, as_of=run_date, industry_map=feature_store.load_industry_map()
    )
    if not features.get("entities"):
        return {"statusCode": 200, "body": json.dumps({"status": "no_features"})}

    cands = nominate(features, recent_narratives=recent, as_of=run_date)

    keys: list[str] = []
    outliers = {c["entity"] for c in cands}
    for cand in cands:
        key = _write(build_narrative(cand), digests_bucket, s3)
        if key:
            keys.append(key)

    # Same-day retraction: an entity active today that is NO LONGER a peer outlier
    # (recomputed |peer_z| back within threshold) has any card written earlier today
    # invalidated. Prior days stay as history.
    z_thr = _f("ONCA_COMPARATIVE_Z", PEER_Z)
    min_peers = int(_f("ONCA_COMPARATIVE_MIN_PEERS", MIN_PEERS))
    recency = _f("ONCA_COMPARATIVE_RECENCY_DAYS", RECENCY_DAYS)
    cohorts = features.get("cohorts") or {}
    normalized: set[str] = set()
    for e in features.get("entities") or []:
        ent = e.get("entity")
        if not ent or ent in outliers or (e.get("days_since_last") or 99) > recency:
            continue
        picked = _best_cohort(e, cohorts, min_peers)
        if picked is None or abs(picked[1]) < z_thr:
            normalized.add(ent)
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
                "entities": [f"{c['entity']}:{c['direction']}({c['cohort']})" for c in cands],
                "keys": keys,
                "retracted": sorted(retracted),
            }
        ),
    }


def _retract_same_day(bucket: str, s3: Any, run_date: str, normalized: set[str]) -> list[str]:
    out: list[str] = []
    for ent in sorted(normalized):
        key = f"{feature_store.NARRATIVES_PREFIX}{run_date}/comparative-{ent}.json"
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except Exception:
            continue
        try:
            s3.delete_object(Bucket=bucket, Key=key)
            out.append(ent)
        except Exception as exc:  # pragma: no cover - best-effort
            print(f"Warning: retract comparative card {key} failed: {exc}")
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
        print(f"Warning: write comparative narrative failed: {exc}")
        return None
