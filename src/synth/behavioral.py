"""Wave 2 (ADR 003) — the Behavioral / campaign axis: an entity's activity signature.

Where the other axes describe *what happened*, this describes *how an entity behaves* —
recurring patterns in its own activity, matched against **pattern templates** (ADR's
"(entity, pattern template)"). It is deliberately about cross-cutting posture, NOT a
single event arc (that is the threaded/incident axis), so it never duplicates a thread:

- **drumbeat** — a regular cadence of signals (`cadence_regular` from the feature store):
  a steady operating rhythm / continuous-presence campaign.
- **multi-front** — activity spread across many distinct event types in the window
  (M&A + lançamento + parceria + …): a broad, aggressive campaign posture.

Design (deterministic, no LLM; mirrors the Wave-1 detectors):
- Nominates on the feature store (cadence) + the event-type taxonomy over the narrative
  history (breadth); the card is a heuristic pt-BR briefing.
- **Grounded inference** — cites the entity's recent driving activity while labeled
  inference (`is_inference` / `mode="derived"` / `axis="behavioral"`).
- **SWOT feeder (ADR 004)** — a sustained campaign / broad posture is an execution
  **Strength** (`swot_hint` S).
- **No feedback loop** (`behavioral` ∈ DERIVED_AXES); pattern-tuned emit-on-change +
  same-day retract, like the other detectors. The ADR rates this the weakest value/
  effort axis, so v1 is intentionally two templates, not a template zoo.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

from src.synth import feature_store, longitudinal, threads
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

AXIS = "behavioral"

# --- Gate (env-overridable) -------------------------------------------------
MIN_ACTIVE_DAYS = 4      # need a real activity history for a behavioral read
DRUMBEAT_MAX_GAP = 21    # a "drumbeat" is a *frequent* regular cadence (<= this gap)
MIN_FRONTS = 3           # multi-front: >= this many distinct event types
RECENCY_DAYS = 14        # the pattern must be current (entity active within this)
COOLDOWN_DAYS = 10       # behavioral patterns are slow — re-fire sparingly


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return float(default)


def _days_between(a: str, b: str) -> int | None:
    try:
        return (feature_store._parse(a) - feature_store._parse(b)).days
    except Exception:
        return None


def event_type_fronts(narratives: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per entity, the distinct event types seen in its ACTIVITY narratives."""
    out: dict[str, dict[str, Any]] = {}
    for n in narratives:
        if not feature_store.is_activity_narrative(n):
            continue
        ent = n.get("entity")
        if not ent:
            continue
        rec = out.setdefault(ent, {"types": set()})
        for et in threads.event_types_of(n):
            rec["types"].add(et)
    return out


def prior_patterns(narratives: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Most-recent prior behavioral card per (entity, pattern): {(ent,pattern): {run_date}}."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for n in narratives:
        if not isinstance(n, dict) or n.get("axis") != AXIS:
            continue
        ent = n.get("entity")
        pat = n.get("pattern")
        rd = feature_store._date_of(n)
        if not ent or not pat or not rd:
            continue
        key = (ent, pat)
        prev = out.get(key)
        if prev is None or rd > prev["run_date"]:
            out[key] = {"run_date": rd}
    return out


def nominate(
    features: dict[str, Any],
    narratives: list[dict[str, Any]],
    *,
    as_of: str | None = None,
    cooldown_days: int | None = None,
) -> list[dict[str, Any]]:
    """Pure gate: features + history -> behavioral-pattern candidates (no I/O)."""
    as_of = as_of or features.get("as_of") or run_date_today()
    cooldown_days = int(
        cooldown_days if cooldown_days is not None
        else _f("ONCA_BEHAVIORAL_COOLDOWN_DAYS", COOLDOWN_DAYS)
    )
    min_active = int(_f("ONCA_BEHAVIORAL_MIN_ACTIVE_DAYS", MIN_ACTIVE_DAYS))
    max_gap = _f("ONCA_BEHAVIORAL_DRUMBEAT_MAX_GAP", DRUMBEAT_MAX_GAP)
    min_fronts = int(_f("ONCA_BEHAVIORAL_MIN_FRONTS", MIN_FRONTS))
    recency = _f("ONCA_BEHAVIORAL_RECENCY_DAYS", RECENCY_DAYS)

    fronts = event_type_fronts(narratives)
    driver_map = longitudinal.drivers(narratives)
    prior = prior_patterns(narratives)
    feats = {e["entity"]: e for e in features.get("entities") or []}

    def _fresh(ent: str, e: dict[str, Any]) -> bool:
        dsl = e.get("days_since_last")
        return dsl is not None and dsl <= recency

    cands: list[dict[str, Any]] = []
    for ent, e in feats.items():
        if (e.get("active_days") or 0) < min_active or not _fresh(ent, e):
            continue
        label = e.get("label") or ENTITY_LABELS.get(ent, str(ent).title())
        driver = driver_map.get(ent, {}).get("narrative")

        # drumbeat — a frequent, regular operating cadence
        mean_gap = e.get("mean_gap_days")
        if e.get("cadence_regular") and mean_gap is not None and mean_gap <= max_gap:
            cands.append({
                "entity": ent, "label": label, "pattern": "drumbeat",
                "mean_gap_days": float(mean_gap), "active_days": int(e.get("active_days") or 0),
                "last_seen": e.get("last_seen"), "driver": driver,
            })

        # multi-front — a broad campaign posture across many event types
        types = sorted(fronts.get(ent, {}).get("types", set()))
        if len(types) >= min_fronts:
            cands.append({
                "entity": ent, "label": label, "pattern": "multi_front",
                "fronts": types, "n_fronts": len(types),
                "active_days": int(e.get("active_days") or 0),
                "last_seen": e.get("last_seen"), "driver": driver,
            })

    # emit-on-change: suppress a pattern re-seen within the cooldown
    out = []
    for c in cands:
        prev = prior.get((c["entity"], c["pattern"]))
        if prev is not None:
            gap = _days_between(as_of, prev["run_date"])
            if gap is not None and gap < cooldown_days:
                continue
        out.append(c)
    out.sort(key=lambda c: (c["pattern"] == "multi_front", c.get("n_fronts", 0),
                            c["active_days"]), reverse=True)
    return out


def swot_hint(cand: dict[str, Any]) -> dict[str, Any]:
    """ADR 004: a sustained campaign / broad posture is an execution Strength."""
    return {"dimension": "S", "sign": "+", "pattern": cand["pattern"]}


def _behavioral_score(cand: dict[str, Any]) -> float:
    if cand["pattern"] == "multi_front":
        return round(min(0.5, 0.25 + 0.05 * cand["n_fronts"]), 3)
    return 0.3  # drumbeat: steady-presence watch signal


def build_narrative(cand: dict[str, Any]) -> dict[str, Any]:
    """Heuristic pt-BR behavioral-pattern card — labeled inference, cites the driver."""
    label = cand["label"]
    driver = cand.get("driver") or {}
    citations = [c for c in (driver.get("citations") or []) if isinstance(c, dict)]
    score = _behavioral_score(cand)

    if cand["pattern"] == "drumbeat":
        head = (
            f"Padrão de comportamento — cadência regular: {label} mantém um ritmo "
            f"constante de sinais (~{cand['mean_gap_days']:g} dias entre atividades, "
            f"{cand['active_days']} dias ativos). Postura de presença contínua."
        )
    else:
        fronts_pt = ", ".join(threads.EVENT_TYPES[t]["label"] for t in cand["fronts"])
        head = (
            f"Padrão de comportamento — múltiplas frentes: {label} atua em "
            f"{cand['n_fronts']} frentes no período ({fronts_pt}). Postura de campanha ampla."
        )
    driver_txt = (driver.get("narrative") or "").strip()
    if driver_txt:
        head += f" Atividade recente: {driver_txt[:200]}"
    tail = (
        " Padrão derivado do histórico de atividade do próprio concorrente (inferência "
        "de comportamento, não um fato novo) — os sinais citados são a evidência."
    )

    return {
        "id": f"behavioral-{cand['entity']}-{cand['pattern']}",
        "kind": "behavioral",
        "axis": AXIS,
        "subject_type": "entity",
        "pattern": cand["pattern"],
        "entity": cand["entity"],
        "entities": [cand["entity"]],
        "lenses": list(driver.get("lenses") or []),
        "is_alert": False,
        "is_inference": True,
        "threat_score": score,
        "threat_factors": {
            "pattern": cand["pattern"],
            "n_fronts": cand.get("n_fronts"),
            "mean_gap_days": cand.get("mean_gap_days"),
            "active_days": cand["active_days"],
        },
        "threat_score_note": "estimated_v1_behavioral",
        "swot_hint": swot_hint(cand),
        "narrative": head + tail,
        "citations": citations,
        "source_ids": list(driver.get("source_ids") or []),
        "mode": "derived",
        "run_date": run_date_today(),
        "run_at": run_at_now(),
        "as_of": run_date_today(),
        "data_as_of": {"last_activity": cand["last_seen"]},
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Recompute fresh features + history, emit behavioral-pattern narratives."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    window_days = int(_f("ONCA_BEHAVIORAL_WINDOW_DAYS", _f("ONCA_FEATURE_WINDOW_DAYS", 90)))
    s3 = boto3.client("s3")
    run_date = run_date_today()

    recent = feature_store.load_history(digests_bucket, window_days, s3=s3)
    features = feature_store.build_features(
        recent, as_of=run_date, industry_map=feature_store.load_industry_map()
    )
    if not features.get("entities"):
        return {"statusCode": 200, "body": json.dumps({"status": "no_features"})}

    cands = nominate(features, recent, as_of=run_date)
    fired = {(c["entity"], c["pattern"]) for c in cands}

    keys: list[str] = []
    for cand in cands:
        key = _write(build_narrative(cand), digests_bucket, s3)
        if key:
            keys.append(key)

    # Same-day retraction: a behavioral card written today whose pattern no longer
    # holds (recomputed) is invalidated. Compute the RAW pattern set (ignoring
    # cooldown) and retract today's cards not in it.
    raw = {(c["entity"], c["pattern"]) for c in nominate(features, recent, as_of=run_date, cooldown_days=0)}
    retracted = _retract_same_day(digests_bucket, s3, run_date, raw)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "ok",
            "as_of": run_date,
            "window_days": window_days,
            "nominated": len(cands),
            "emitted": len(keys),
            "patterns": [f"{c['entity']}:{c['pattern']}" for c in cands],
            "keys": keys,
            "retracted": sorted(f"{e}:{p}" for e, p in retracted),
        }),
    }


def _retract_same_day(bucket: str, s3: Any, run_date: str, raw: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Delete same-day behavioral cards whose pattern no longer holds."""
    prefix = f"{feature_store.NARRATIVES_PREFIX}{run_date}/behavioral-"
    out: list[tuple[str, str]] = []
    try:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    except Exception as exc:  # pragma: no cover - best-effort
        print(f"Warning: list behavioral cards failed: {exc}")
        return out
    for obj in resp.get("Contents") or []:
        stem = obj["Key"][len(prefix):].removesuffix(".json")  # {entity}-{pattern}
        ent, _, pat = stem.rpartition("-")
        if (ent, pat) in raw:
            continue
        try:
            s3.delete_object(Bucket=bucket, Key=obj["Key"])
            out.append((ent, pat))
        except Exception as exc:  # pragma: no cover - best-effort
            print(f"Warning: retract behavioral card {obj['Key']} failed: {exc}")
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
        print(f"Warning: write behavioral narrative failed: {exc}")
        return None
