"""Wave 1 (ADR 003) — the Cohort / vintage axis: how a whole segment is faring.

The subject is a **set** — an industry cohort — narrated **over time**. Where
comparative asks "is THIS entity an outlier vs its cohort right now?" (cross-sectional)
and longitudinal asks "did THIS entity break its own trend?", cohort asks
"is the WHOLE segment breaking its own trend?" — set-longitudinal:

- **heating** — the cohort's aggregate activity/threat is running well above its own
  recent baseline (the segment as a whole is intensifying),
- **cooling** — the aggregate has fallen well below baseline.

Design (per ADR 003 — "set-longitudinal over registry + feature store"):
- **Deterministic, no LLM.** A per-day cohort "temperature" (mean of member peak
  threats that day) is built from the durable narrative history + the registry
  industry map; the recent window is z-scored against the cohort's own baseline.
- **Vintage flavor.** The card names how many of the recently-active members are **new
  entrants** (first seen inside the recent window) — a light read on segment vintage
  without over-claiming a real founding date we don't hold.
- **Grounded inference.** Cites the driving member activity while labeled inference
  (`is_inference` / `mode="derived"` / `axis="cohort"`).
- **SWOT feeder (ADR 004).** A heating segment is more competitive intensity — a
  **Threat** on the client (dim "T"); a cooling segment an **Opportunity** (dim "O").
- **No feedback loop.** `cohort` is a derived axis (excluded from feature-store activity).
- **Segment-tuned emit-on-change.** A standing move is suppressed within a cooldown
  unless it escalates a tier; a segment that reverts is retracted same-day.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

from src.synth import feature_store
from src.synth.synthesize import run_at_now, run_date_today

AXIS = "cohort"

# --- Nomination gate (env-overridable) -------------------------------------
# Window-vs-window (robust on thin history): the recent window's member-day threat
# observations are tested against the baseline window's distribution.
MIN_MEMBERS = 3        # a cohort needs >= this many distinct active members
MIN_RECENT_OBS = 3     # ... >= this many member-day observations in the recent window
MIN_BASE_OBS = 5       # ... and >= this many in the baseline window (a real baseline)
RECENT_DAYS = 7        # the "recent" window whose aggregate is tested
Z_THRESHOLD = 1.5      # |cohort_z| beyond this is a segment move
MIN_MOVE = 0.1         # ... and the absolute temperature move must be material
COOLDOWN_DAYS = 7      # re-emit suppression: only an escalated tier re-fires


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


def move_tier(abs_z: float) -> int:
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


def prior_moves(narratives: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Most-recent prior cohort card per cohort: {slug: {run_date, tier, direction}}."""
    out: dict[str, dict[str, Any]] = {}
    for n in narratives:
        if not isinstance(n, dict) or n.get("axis") != AXIS:
            continue
        slug = n.get("cohort")
        rd = feature_store._date_of(n)
        if not slug or not rd:
            continue
        prev = out.get(slug)
        if prev is None or rd > prev["run_date"]:
            out[slug] = {
                "run_date": rd,
                "tier": int(n.get("move_tier") or 1),
                "direction": n.get("direction"),
            }
    return out


def cohort_series(
    narratives: list[dict[str, Any]], industry_map: dict[str, list[str]], *, as_of: str
) -> dict[str, dict[str, Any]]:
    """Per industry cohort: member-day threat observations + membership + drivers.

    One observation per (member, active date) using that day's PEAK threat, so a
    segment where several members fire hot reads hotter than a quiet one. Aggregated
    window-vs-window in ``nominate`` rather than as a per-day series (robust on the
    thin daily history: more observations behind each window's mean).
    """
    obs: dict[str, dict[tuple[str, str], float]] = {}   # slug -> {(entity,date): peak}
    members: dict[str, set[str]] = {}
    first_seen: dict[str, str] = {}
    cards_by_slug_date: dict[str, dict[str, list[dict]]] = {}
    for n in narratives:
        if not feature_store.is_activity_narrative(n):
            continue
        ent = n.get("entity")
        date = feature_store._date_of(n)
        if not ent or not date:
            continue
        peak = _score(n.get("threat_score"))
        if ent not in first_seen or date < first_seen[ent]:
            first_seen[ent] = date
        for slug in industry_map.get(ent, []):
            o = obs.setdefault(slug, {})
            k = (ent, date)
            o[k] = max(o.get(k, 0.0), peak)
            members.setdefault(slug, set()).add(ent)
            cards_by_slug_date.setdefault(slug, {}).setdefault(date, []).append(n)

    out: dict[str, dict[str, Any]] = {}
    for slug, o in obs.items():
        observations = [{"entity": e, "date": d, "peak": p} for (e, d), p in o.items()]
        out[slug] = {
            "observations": observations,
            "members": members.get(slug, set()),
            "first_seen": first_seen,
            "cards": cards_by_slug_date.get(slug, {}),
        }
    return out


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def nominate(
    narratives: list[dict[str, Any]],
    industry_map: dict[str, list[str]],
    *,
    as_of: str | None = None,
    cooldown_days: int | None = None,
) -> list[dict[str, Any]]:
    """Pure gate: recent narratives + industry map -> segment-move candidates."""
    as_of = as_of or run_date_today()
    cooldown_days = int(
        cooldown_days if cooldown_days is not None
        else _f("ONCA_COHORT_COOLDOWN_DAYS", COOLDOWN_DAYS)
    )
    min_members = int(_f("ONCA_COHORT_MIN_MEMBERS", MIN_MEMBERS))
    min_recent = int(_f("ONCA_COHORT_MIN_RECENT_OBS", MIN_RECENT_OBS))
    min_base = int(_f("ONCA_COHORT_MIN_BASE_OBS", MIN_BASE_OBS))
    recent_days = int(_f("ONCA_COHORT_RECENT_DAYS", RECENT_DAYS))
    z_thr = _f("ONCA_COHORT_Z", Z_THRESHOLD)
    min_move = _f("ONCA_COHORT_MIN_MOVE", MIN_MOVE)

    series_map = cohort_series(narratives, industry_map, as_of=as_of)
    prior = prior_moves(narratives)

    out: list[dict[str, Any]] = []
    for slug, data in series_map.items():
        obs = data["observations"]
        if len(data["members"]) < min_members:
            continue

        recent = [o for o in obs if (g := _days_between(as_of, o["date"])) is not None and 0 <= g <= recent_days]
        baseline = [o for o in obs if o not in recent]
        if len(recent) < min_recent or len(baseline) < min_base:
            continue

        recent_temp = sum(o["peak"] for o in recent) / len(recent)
        base_mean, base_std = feature_store._mean_std([o["peak"] for o in baseline])
        z = (recent_temp - base_mean) / max(base_std, feature_store.MIN_STD)
        if abs(recent_temp - base_mean) < min_move or abs(z) < z_thr:
            continue

        direction = "heating" if z > 0 else "cooling"
        tier = move_tier(abs(z))

        prev = prior.get(slug)
        if prev is not None and prev.get("direction") == direction:
            gap = _days_between(as_of, prev["run_date"])
            if gap is not None and gap < cooldown_days and tier <= prev["tier"]:
                continue

        recent_members = sorted({o["entity"] for o in recent})
        new_members = [
            e for e in recent_members
            if (g := _days_between(as_of, data["first_seen"].get(e, "1900-01-01"))) is not None
            and 0 <= g <= recent_days
        ]
        recent_dates = {o["date"] for o in recent}
        recent_cards = [c for d, cs in data["cards"].items() if d in recent_dates for c in cs]
        drivers = sorted(
            recent_cards,
            key=lambda n: (_score(n.get("threat_score")), feature_store._date_of(n)),
            reverse=True,
        )[:3]

        out.append(
            {
                "cohort": slug,
                "cohort_label": _industry_label(slug),
                "direction": direction,
                "cohort_z": round(float(z), 2),
                "recent_temp": round(recent_temp, 3),
                "baseline_temp": round(base_mean, 3),
                "members": len(data["members"]),
                "recent_members": recent_members,
                "new_members": new_members,
                "recent_obs": len(recent),
                "baseline_obs": len(baseline),
                "latest": max(o["date"] for o in obs),
                "move_tier": tier,
                "drivers": drivers,
            }
        )
    out.sort(
        key=lambda c: (c["direction"] == "heating", c["move_tier"], abs(c["cohort_z"])),
        reverse=True,
    )
    return out


def _move_score(cand: dict[str, Any]) -> float:
    """Heating reports the segment's real elevated temperature (capped context);
    cooling is a low watch signal."""
    if cand["direction"] == "heating":
        return round(min(0.55, cand["recent_temp"]), 3)
    return round(min(0.3, 0.12 + cand["recent_temp"]), 3)


def swot_hint(cand: dict[str, Any]) -> dict[str, Any]:
    """ADR 004: a heating segment is a Threat (more intensity); cooling an Opportunity."""
    heating = cand["direction"] == "heating"
    return {
        "dimension": "T" if heating else "O",
        "sign": "-" if heating else "+",
        "cohort": cand["cohort"],
        "entities": cand["recent_members"],
    }


def _agg_citations(drivers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for d in drivers:
        for c in d.get("citations") or []:
            if not isinstance(c, dict):
                continue
            key = c.get("url") or c.get("id") or json.dumps(c, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
    return out


def build_narrative(cand: dict[str, Any]) -> dict[str, Any]:
    """Heuristic pt-BR segment-trajectory card — labeled inference, cites drivers."""
    coh = cand["cohort_label"]
    z = cand["cohort_z"]
    rt = cand["recent_temp"]
    bt = cand["baseline_temp"]
    n_mem = cand["members"]
    n_new = len(cand["new_members"])
    score = _move_score(cand)
    drivers = cand.get("drivers") or []
    citations = _agg_citations(drivers)
    source_ids: list[str] = []
    for d in drivers:
        source_ids.extend(d.get("source_ids") or [])

    if cand["direction"] == "heating":
        head = (
            f"Movimento de cohort (aquecimento): o segmento {coh} como um todo está "
            f"ACIMA da própria média — temperatura {rt:.2f} vs {bt:.2f} de base "
            f"(z={z:+.1f}, {n_mem} players)."
        )
    else:
        head = (
            f"Movimento de cohort (esfriamento): o segmento {coh} como um todo está "
            f"ABAIXO da própria média — temperatura {rt:.2f} vs {bt:.2f} de base "
            f"(z={z:+.1f}, {n_mem} players)."
        )
    if n_new:
        head += f" Inclui {n_new} novo(s) entrante(s) no período."
    lead = (drivers[0].get("narrative") or "").strip() if drivers else ""
    if lead:
        head += f" Puxando o movimento: {lead[:200]}"
    tail = (
        " Índice agregado da trajetória do segmento inteiro (inferência de cohort, não "
        "um fato novo de uma entidade) — os sinais dos membros são a evidência."
    )

    return {
        "id": f"cohort-{cand['cohort']}",
        "kind": "cohort",
        "axis": AXIS,
        "subject_type": "set",
        "direction": cand["direction"],
        "cohort": cand["cohort"],
        "cohort_label": coh,
        "entity": None,
        "entities": list(cand["recent_members"]),
        "new_members": list(cand["new_members"]),
        "lenses": ["market"],
        "is_alert": False,
        "is_inference": True,
        "threat_score": score,
        "threat_factors": {
            "cohort_z": z,
            "direction": cand["direction"],
            "recent_temp": rt,
            "baseline_temp": bt,
            "members": n_mem,
            "move_tier": cand["move_tier"],
        },
        "threat_score_note": "estimated_v1_cohort",
        "move_tier": cand["move_tier"],
        "cohort_z": z,
        "swot_hint": swot_hint(cand),
        "narrative": head + tail,
        "citations": citations,
        "source_ids": source_ids,
        "mode": "derived",
        "run_date": run_date_today(),
        "run_at": run_at_now(),
        "as_of": run_date_today(),
        "data_as_of": {"latest_activity": cand["latest"]},
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Load recent history + industry map, emit segment-trajectory narratives."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    window_days = int(_f("ONCA_COHORT_WINDOW_DAYS", _f("ONCA_FEATURE_WINDOW_DAYS", 90)))
    s3 = boto3.client("s3")
    run_date = run_date_today()

    recent = feature_store.load_history(digests_bucket, window_days, s3=s3)
    industry_map = feature_store.load_industry_map()
    cands = nominate(recent, industry_map, as_of=run_date)

    keys: list[str] = []
    moving = {c["cohort"] for c in cands}
    for cand in cands:
        key = _write(build_narrative(cand), digests_bucket, s3)
        if key:
            keys.append(key)

    # Same-day retraction: a cohort card written today whose segment reverted (no longer
    # a move) is invalidated. Recompute the raw move set (ignoring cooldown) and retract
    # any today card not in it.
    z_thr = _f("ONCA_COHORT_Z", Z_THRESHOLD)
    still_moving = {
        c["cohort"] for c in nominate(recent, industry_map, as_of=run_date, cooldown_days=0)
    }
    prefix = f"{feature_store.NARRATIVES_PREFIX}{run_date}/cohort-"
    retracted: list[str] = []
    try:
        resp = s3.list_objects_v2(Bucket=digests_bucket, Prefix=prefix)
        for obj in resp.get("Contents") or []:
            slug = obj["Key"][len(prefix):].removesuffix(".json")
            if slug in still_moving:
                continue
            s3.delete_object(Bucket=digests_bucket, Key=obj["Key"])
            retracted.append(slug)
    except Exception as exc:  # pragma: no cover - best-effort
        print(f"Warning: cohort retract failed: {exc}")

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "status": "ok",
                "as_of": run_date,
                "window_days": window_days,
                "nominated": len(cands),
                "emitted": len(keys),
                "cohorts": [f"{c['cohort']}:{c['direction']}({c['members']})" for c in cands],
                "keys": keys,
                "retracted": sorted(retracted),
            }
        ),
    }


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
        print(f"Warning: write cohort narrative failed: {exc}")
        return None
