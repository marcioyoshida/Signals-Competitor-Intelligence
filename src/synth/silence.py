"""Wave 1 (ADR 003) — the Silence axis: who went quiet.

The first user-facing consumer of the Wave 0 feature store. It inverts
emit-on-change: instead of narrating a *new* signal, it narrates the **absence**
of an expected one — an entity that has gone silent relative to its own
established cadence (``mean_gap_days`` / ``cadence_regular`` from
``features/latest.json``).

Design (per ADR 003):
- **Deterministic nominator, no LLM.** The gate is arithmetic on the feature
  store; the card is a heuristic pt-BR briefing. Zero defamation/PII surface — it
  asserts an absence about one already-tracked entity, not a relationship.
- **Inference, labeled — never a sourced claim.** Silence has no source URL (it is
  the lack of one), so these cards bypass the citation guardrail by design and are
  stamped ``is_inference`` / ``mode="derived"`` / ``axis="silence"``. Decision 4:
  inference is flagged, not asserted as fact.
- **Absence needs its own emit-on-change.** A standing silence would otherwise
  re-fire every run. We suppress within a cooldown unless the silence *escalates*
  to a higher tier (``silence_tier`` read back off prior silence cards).
- **No feedback loop.** Silence cards are written to ``narratives/`` like any
  other, so the feature store must NOT count them as activity (else emitting a
  silence would reset the entity's ``days_since_last``). Freshness here is
  recomputed from *activity* narratives only, via
  ``feature_store.is_activity_narrative``.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

from src.synth import feature_store
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

AXIS = "silence"

# --- Nomination gate (env-overridable) -------------------------------------
# Need a real cadence before "quiet" means anything.
MIN_ACTIVE_DAYS = 3
# Absolute floor: never call a gap shorter than this "silence", however tight
# the cadence (a daily-drummer skipping two days is noise, not a story).
MIN_SILENCE_DAYS = 7
# days_since_last must exceed this multiple of the typical gap ...
SILENCE_MULT = 2.5
# ... and clear the typical gap by at least this absolute margin (guards
# tiny-cadence entities from tripping on the multiple alone).
MIN_GAP_MARGIN_DAYS = 3
# Re-emit suppression: within this many days, only escalation re-fires.
SILENCE_COOLDOWN_DAYS = 5
# Silence is a *watch* signal, never a top-tier alert on its own.
MAX_SILENCE_SCORE = 0.6


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return float(default)


def silence_tier(ratio: float) -> int:
    """Escalation tier from the overrun ratio (days_since_last / mean_gap_days)."""
    if ratio >= 6:
        return 3
    if ratio >= 4:
        return 2
    return 1


def _days_between(a: str, b: str) -> int | None:
    try:
        return (feature_store._parse(a) - feature_store._parse(b)).days
    except Exception:
        return None


def activity_freshness(
    narratives: list[dict[str, Any]], *, as_of: str
) -> dict[str, tuple[str, int]]:
    """Map entity -> (last_activity_date, days_since_last) from ACTIVITY narratives.

    Recomputed here (not trusted from the possibly-lagging features snapshot) so an
    entity that fired *today* — after the pre-synth feature build — is correctly
    seen as active and never flagged silent. Derived/inference cards are excluded.
    """
    last: dict[str, str] = {}
    for n in narratives:
        if not feature_store.is_activity_narrative(n):
            continue
        ent = n.get("entity")
        date = feature_store._date_of(n)
        if not ent or not date:
            continue
        if ent not in last or date > last[ent]:
            last[ent] = date
    out: dict[str, tuple[str, int]] = {}
    for ent, date in last.items():
        d = _days_between(as_of, date)
        if d is not None:
            out[ent] = (date, d)
    return out


def prior_silences(narratives: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Most-recent prior silence card per entity: {entity: {run_date, tier}}."""
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
            out[ent] = {"run_date": rd, "tier": int(n.get("silence_tier") or 1)}
    return out


def nominate(
    features: dict[str, Any],
    *,
    recent_narratives: list[dict[str, Any]] | None = None,
    as_of: str | None = None,
    cooldown_days: int | None = None,
) -> list[dict[str, Any]]:
    """Pure gate: feature store -> silence candidates (deterministic, no I/O).

    An entity is nominated when it has an established cadence yet has been quiet
    well past it, and it is not already covered by a recent, un-escalated silence.
    """
    recent_narratives = recent_narratives or []
    as_of = as_of or features.get("as_of") or run_date_today()
    cooldown_days = int(
        cooldown_days
        if cooldown_days is not None
        else _f("ONCA_SILENCE_COOLDOWN_DAYS", SILENCE_COOLDOWN_DAYS)
    )
    min_active = int(_f("ONCA_SILENCE_MIN_ACTIVE_DAYS", MIN_ACTIVE_DAYS))
    min_days = _f("ONCA_SILENCE_MIN_DAYS", MIN_SILENCE_DAYS)
    mult = _f("ONCA_SILENCE_MULT", SILENCE_MULT)
    margin = _f("ONCA_SILENCE_GAP_MARGIN_DAYS", MIN_GAP_MARGIN_DAYS)

    fresh = activity_freshness(recent_narratives, as_of=as_of)
    prior = prior_silences(recent_narratives)

    out: list[dict[str, Any]] = []
    for e in features.get("entities") or []:
        ent = e.get("entity")
        mean_gap = e.get("mean_gap_days")
        if not ent or mean_gap is None or (e.get("active_days") or 0) < min_active:
            continue

        # Freshness: prefer the lag-free recompute; fall back to the snapshot.
        if ent in fresh:
            last_seen, days_since = fresh[ent]
        else:
            last_seen = e.get("last_seen")
            days_since = e.get("days_since_last")
        if last_seen is None or days_since is None:
            continue

        threshold = max(min_days, mult * mean_gap, mean_gap + margin)
        if days_since < threshold:
            continue

        ratio = days_since / mean_gap if mean_gap else 0.0
        tier = silence_tier(ratio)

        # Absence emit-on-change: skip if recently emitted and not escalated.
        prev = prior.get(ent)
        if prev is not None:
            gap = _days_between(as_of, prev["run_date"])
            if gap is not None and gap < cooldown_days and tier <= prev["tier"]:
                continue

        out.append(
            {
                "entity": ent,
                "label": e.get("label") or ENTITY_LABELS.get(ent, str(ent).title()),
                "days_since_last": int(days_since),
                "last_seen": last_seen,
                "mean_gap_days": float(mean_gap),
                "active_days": int(e.get("active_days") or 0),
                "cadence_regular": bool(e.get("cadence_regular")),
                "score_mean": float(e.get("score_mean") or 0.0),
                "industries": list(e.get("industries") or []),
                "ratio": round(ratio, 2),
                "silence_tier": tier,
            }
        )
    # Sharpest, longest silences first.
    out.sort(key=lambda c: (c["silence_tier"], c["ratio"], c["days_since_last"]), reverse=True)
    return out


def _silence_score(cand: dict[str, Any]) -> float:
    """Watch-level threat: baseline importance nudged by how far past cadence.

    A quiet major player (high historical ``score_mean``) matters more than a
    quiet minor one; capped so silence never headlines above a real alert.
    """
    base = 0.25 + 0.12 * cand["silence_tier"] + 0.40 * cand["score_mean"]
    return round(min(MAX_SILENCE_SCORE, base), 3)


def build_narrative(cand: dict[str, Any]) -> dict[str, Any]:
    """Heuristic pt-BR silence card — labeled inference, no URL, no citations."""
    label = cand["label"]
    days = cand["days_since_last"]
    gap = cand["mean_gap_days"]
    cadence = "regular" if cand["cadence_regular"] else "irregular"
    score = _silence_score(cand)
    text = (
        f"Silêncio de sinal: {label} não emite sinais rastreáveis há {days} dias, "
        f"contra uma cadência típica de ~{gap:g} dias ({cand['active_days']} dias "
        f"ativos na janela, padrão {cadence}). Última atividade observada em "
        f"{cand['last_seen']}. Indicador derivado (ausência de sinal), não um fato "
        f"reportado — vale monitorar se reflete recuo estratégico, sazonalidade ou "
        f"lacuna de cobertura."
    )
    return {
        "id": f"silence-{cand['entity']}",
        "kind": "silence",
        "axis": AXIS,
        "subject_type": "entity",
        "entity": cand["entity"],
        "entities": [cand["entity"]],
        "lenses": [],
        "is_alert": False,
        "is_inference": True,
        "threat_score": score,
        "threat_factors": {
            "silence_tier": cand["silence_tier"],
            "overrun_ratio": cand["ratio"],
            "baseline_importance": round(cand["score_mean"], 3),
        },
        "threat_score_note": "estimated_v1_silence",
        "silence_tier": cand["silence_tier"],
        "days_since_last": days,
        "mean_gap_days": gap,
        "last_seen": cand["last_seen"],
        "narrative": text,
        "citations": [],
        "source_ids": [],
        "mode": "derived",
        "run_date": run_date_today(),
        "run_at": run_at_now(),
        "as_of": run_date_today(),
        "data_as_of": {"last_activity": cand["last_seen"]},
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Read features/latest.json + recent activity, emit silence narratives."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    window_days = int(_f("ONCA_SILENCE_WINDOW_DAYS", _f("ONCA_FEATURE_WINDOW_DAYS", 90)))
    s3 = boto3.client("s3")

    features = feature_store.load_features(digests_bucket, s3=s3)
    if not features.get("entities"):
        return {"statusCode": 200, "body": json.dumps({"status": "no_features"})}

    recent = feature_store.load_history(digests_bucket, window_days, s3=s3)
    run_date = run_date_today()
    cands = nominate(features, recent_narratives=recent, as_of=run_date)

    keys: list[str] = []
    for cand in cands:
        narrative = build_narrative(cand)
        key = _write(narrative, digests_bucket, s3)
        if key:
            keys.append(key)

    # Same-day retraction: an entity that produced REAL activity today cannot be
    # silent today. If an earlier run (e.g. 06:45, before the news landed) wrote a
    # silence card for it, that card is now false — retract it. Derived cards are
    # recomputable, so removing a superseded same-day one is legitimate; prior
    # days are left untouched as history.
    fresh = activity_freshness(recent, as_of=run_date)
    recovered = {ent for ent, (date, _dsl) in fresh.items() if date == run_date}
    retracted = _retract_same_day(digests_bucket, s3, run_date, recovered)

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "status": "ok",
                "as_of": features.get("as_of"),
                "window_days": window_days,
                "nominated": len(cands),
                "emitted": len(keys),
                "entities": [c["entity"] for c in cands],
                "keys": keys,
                "retracted": retracted,
            }
        ),
    }


def _retract_same_day(bucket: str, s3: Any, run_date: str, recovered: set[str]) -> list[str]:
    """Delete same-day silence cards for entities that became active today."""
    out: list[str] = []
    for ent in sorted(recovered):
        key = f"{feature_store.NARRATIVES_PREFIX}{run_date}/silence-{ent}.json"
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except Exception:
            continue  # no same-day silence card for this entity — nothing to retract
        try:
            s3.delete_object(Bucket=bucket, Key=key)
            out.append(ent)
        except Exception as exc:  # pragma: no cover - best-effort
            print(f"Warning: retract silence card {key} failed: {exc}")
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
        print(f"Warning: write silence narrative failed: {exc}")
        return None
