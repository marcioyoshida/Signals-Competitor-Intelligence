"""Wave 0 (ADR 003) — per-entity feature store: the shared enabler.

Derives rolling per-entity features from the **durable** `narratives/` history
(never from raw — so it is independent of the 395-day KB/raw retention) and
publishes one recomputable snapshot `features/latest.json`. No user-facing axis
and no LLM: this is the deterministic substrate the Wave 1 detectors read —

- `days_since_last` + cadence (`mean_gap_days`/`cadence_regular`)  → *silence*,
- score baseline (`score_mean`/`score_std`/`score_z`)              → *longitudinal break*,
- per-industry `cohorts` aggregate                                 → *comparative peer baseline*,
- `first_seen`/vintage + industries                                → *cohort*.

The snapshot is **overwritten** each run (derived state, not a narrative), so it
does not touch the "narrative store never prunes" discipline.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
from typing import Any

import boto3

NARRATIVES_PREFIX = "narratives/"
FEATURES_KEY = "features/latest.json"

# Derived/inference narratives (ADR 003 axes that assert an *absence* or an
# *inference*, not a fresh source signal). These are written into narratives/
# like any other card, but they must NOT count as entity "activity" — otherwise
# emitting a "went quiet" silence card would itself reset the entity's
# days_since_last and cadence, a feedback loop. Freshness/cadence are built from
# activity narratives only.
DERIVED_AXES = frozenset(
    {"silence", "longitudinal", "comparative", "thematic", "regulatory", "cohort",
     "behavioral"}
)

# Noise floor for the break z-score denominator: threat scores are 0..1, so a
# baseline std below this is treated as ~0.05 to avoid a div-by-zero (a flat
# baseline) or an absurd z from trivially-small variance. ~one tier-width.
MIN_STD = 0.05


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _date_of(n: dict[str, Any]) -> str:
    """Run date (when surfaced) — same rule feed_builder uses for the timeline."""
    return str(n.get("run_date") or n.get("as_of") or "")[:10]


def is_activity_narrative(n: dict[str, Any]) -> bool:
    """True for a real source-grounded signal (counts as entity activity).

    Excludes derived/inference axes (silence and future absence/inference axes),
    so a card *about* an absence never registers as presence. Shared by the
    silence detector's freshness recompute so both layers agree on what counts.
    """
    if not isinstance(n, dict):
        return False
    if n.get("axis") in DERIVED_AXES:
        return False
    if n.get("mode") == "derived" or n.get("is_inference"):
        return False
    return True


def _mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)  # sample std
    return mean, math.sqrt(var)


def build_features(
    narratives: list[dict[str, Any]],
    *,
    as_of: str | None = None,
    window_days: int = 90,
    industry_map: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Pure aggregation: narratives -> per-entity features + cohort rollup.

    One observation per (entity, active date) using that day's *peak* threat
    score, so an entity that fires several narratives on one day counts as one
    data point (a day). Baselines exclude the latest point when computing the
    break z-score, so a point is never scored against itself.
    """
    industry_map = industry_map or {}
    # Only source-grounded activity shapes baselines/cadence (never silence &c).
    narratives = [n for n in narratives if is_activity_narrative(n)]

    # (entity, date) -> {peak, alert, lenses, label}
    by_entity: dict[str, dict[str, Any]] = {}
    for n in narratives:
        if not isinstance(n, dict):
            continue
        ent = n.get("entity")
        date = _date_of(n)
        if not ent or not date:
            continue
        rec = by_entity.setdefault(
            ent, {"label": None, "by_date": {}, "lenses": set(), "alerts": 0}
        )
        if rec["label"] is None:
            rec["label"] = n.get("entity_label") or n.get("label")
        day = rec["by_date"].setdefault(date, {"peak": 0.0, "alert": False})
        day["peak"] = max(day["peak"], _score(n.get("threat_score")))
        if n.get("is_alert"):
            day["alert"] = True
            rec["alerts"] += 1
        for lens in n.get("lenses") or []:
            rec["lenses"].add(lens)

    dates_all = [d for n in narratives if (d := _date_of(n))]
    as_of = as_of or (max(dates_all) if dates_all else dt.date.today().isoformat())
    as_of_d = _parse(as_of)

    entities: list[dict[str, Any]] = []
    for ent, rec in by_entity.items():
        active = sorted(rec["by_date"])  # unique active dates, ascending
        peaks = [rec["by_date"][d]["peak"] for d in active]
        score_mean, score_std = _mean_std(peaks)
        score_last = peaks[-1] if peaks else 0.0

        # break primitive: latest peak vs the baseline of everything before it
        # (never against itself). Denominator floored at MIN_STD; no baseline -> 0.
        base = peaks[:-1]
        if base:
            base_mean, base_std = _mean_std(base)
            score_z = (score_last - base_mean) / max(base_std, MIN_STD)
        else:
            score_z = 0.0

        # cadence: gaps (days) between consecutive active dates
        gaps = [
            (_parse(active[i]) - _parse(active[i - 1])).days
            for i in range(1, len(active))
        ]
        mean_gap, std_gap = _mean_std([float(g) for g in gaps]) if gaps else (None, None)
        cadence_regular = bool(
            mean_gap and len(gaps) >= 3 and std_gap is not None and std_gap <= 0.5 * mean_gap
        )

        last_seen = active[-1] if active else None
        days_since_last = (as_of_d - _parse(last_seen)).days if last_seen else None

        entities.append(
            {
                "entity": ent,
                "label": rec["label"] or ent.replace("_", " ").title(),
                "narratives_total": sum(
                    1 for n in narratives if n.get("entity") == ent and _date_of(n)
                ),
                "active_days": len(active),
                "first_seen": active[0] if active else None,
                "last_seen": last_seen,
                "days_since_last": days_since_last,
                "alerts_total": rec["alerts"],
                "score_mean": round(score_mean, 4),
                "score_std": round(score_std, 4),
                "score_max": round(max(peaks), 4) if peaks else 0.0,
                "score_last": round(score_last, 4),
                "score_z": round(score_z, 4),
                "mean_gap_days": round(mean_gap, 2) if mean_gap is not None else None,
                "std_gap_days": round(std_gap, 2) if std_gap is not None else None,
                "cadence_regular": cadence_regular,
                "lenses": sorted(rec["lenses"]),
                "industries": sorted(industry_map.get(ent, [])),
            }
        )
    entities.sort(key=lambda e: (e["score_max"], e["active_days"]), reverse=True)

    # Cohort rollup (comparative peer baseline material): group entities by each
    # industry they belong to (an entity may count under several).
    cohorts: dict[str, dict[str, Any]] = {}
    for e in entities:
        for slug in e["industries"] or ["_unclassified"]:
            c = cohorts.setdefault(
                slug, {"members": 0, "_means": [], "score_max": 0.0}
            )
            c["members"] += 1
            c["_means"].append(e["score_mean"])
            c["score_max"] = max(c["score_max"], e["score_max"])
    for slug, c in cohorts.items():
        means = c.pop("_means")
        c_mean, c_std = _mean_std(means) if means else (0.0, 0.0)
        c["score_mean"] = round(c_mean, 4)
        # std of member means — the denominator for a peer-outlier z (comparative axis).
        c["score_std"] = round(c_std, 4)

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "as_of": as_of,
        "window_days": window_days,
        "entity_count": len(entities),
        "entities": entities,
        "cohorts": cohorts,
    }


def _parse(iso: str) -> dt.date:
    return dt.date.fromisoformat(str(iso)[:10])


def _recent_dates(window_days: int, as_of: dt.date | None = None) -> set[str]:
    today = as_of or dt.date.today()
    return {(today - dt.timedelta(days=i)).isoformat() for i in range(window_days)}


def load_history(
    bucket: str, window_days: int = 90, *, s3: Any | None = None
) -> list[dict[str, Any]]:
    """Read narrative objects for the last window_days from narratives/{date}/.

    Longer window than the dashboard feed (baselines need history); still bounded
    so the scan stays cheap. Same durable digests store — never raw."""
    s3 = s3 or boto3.client("s3")
    wanted = _recent_dates(window_days)
    out: list[dict[str, Any]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=NARRATIVES_PREFIX):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            parts = key.split("/")
            if len(parts) < 3 or not key.endswith(".json"):
                continue
            if parts[1] not in wanted:
                continue
            try:
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                out.append(json.loads(body.decode("utf-8")))
            except Exception as exc:  # pragma: no cover - skip unreadable objects
                print(f"Warning: skip narrative {key}: {exc}")
    return out


def load_industry_map() -> dict[str, list[str]]:
    """{entity_id: industries} from the registry, best-effort (empty on failure)."""
    if not os.environ.get("ONCA_ENTITIES_TABLE"):
        return {}
    try:
        from src.synth import entity_registry

        return entity_registry.entity_industry_map()
    except Exception as exc:  # pragma: no cover - best-effort
        print(f"Warning: industry map unavailable for features: {exc}")
        return {}


def publish_features(
    features: dict[str, Any], bucket: str, *, key: str = FEATURES_KEY, s3: Any | None = None
) -> str:
    """Overwrite the single derived features snapshot (recomputable, not a narrative)."""
    s3 = s3 or boto3.client("s3")
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(features, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
        CacheControl="no-cache",
    )
    return f"s3://{bucket}/{key}"


def load_features(bucket: str, *, key: str = FEATURES_KEY, s3: Any | None = None) -> dict[str, Any]:
    """Read the latest features snapshot (for Wave 1 consumers/tests). {} if absent."""
    s3 = s3 or boto3.client("s3")
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body.decode("utf-8"))
    except Exception:  # pragma: no cover - absent/unreadable snapshot
        return {}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Build features/latest.json from the durable narrative history."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    window_days = int(os.environ.get("ONCA_FEATURE_WINDOW_DAYS", "90"))
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    narratives = load_history(digests_bucket, window_days)
    features = build_features(
        narratives, window_days=window_days, industry_map=load_industry_map()
    )
    published = None
    try:
        published = publish_features(features, digests_bucket)
    except Exception as exc:  # pragma: no cover - publish is best-effort
        print(f"Warning: features publish failed: {exc}")

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "status": "ok",
                "as_of": features["as_of"],
                "window_days": window_days,
                "entities": features["entity_count"],
                "cohorts": len(features["cohorts"]),
                "published": published,
            }
        ),
    }
