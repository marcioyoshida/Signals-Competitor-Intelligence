"""ADR 004 Phase C — analyst vetting: promote or suppress a queued proposal.

The reconcile (step 3), seed (step 4), and relationship-graph (ADR-003 Wave 3)
producers all emit REVIEW-GATED proposals into durable S3 stores, read-only until a
human decides. This is the decision layer the vetting endpoint calls — approve or
reject one proposal by id, idempotently:

- **SWOT proposals** (`swot/proposals.json` reconcile new/challenge, and
  `swot/seed_proposals.json` cold-start seeds). Approving a *new*/*seed* PROMOTES the
  claim into a durable curated-bullet store (`swot/curated.json`) that the belief
  builder folds into every rebuild — so a human-approved belief SURVIVES the recompute
  (the belief file is derived state, overwritten each run). Approving a *challenge*
  records a retirement that marks the target bullet retired. This is the only path
  that turns an interpretive LLM claim into an asserted (`active`) belief.
- **Graph proposals** (`graph/relational_proposals.json` edges,
  `graph/person_proposals.json` operative persons). Approve/reject records the durable
  status; the producers' `merge_proposals` preserves it, so an approved edge stays and
  a rejected one never returns to the pending queue.

Every decision flips `status` in the proposal's OWN store (so feed_builder drops it
from the pending panel) and is idempotent: deciding an already-decided proposal is a
no-op that reports the standing decision, never a double-promote.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

from src.synth import swot_store

DECISIONS = ("approved", "rejected")

# ADR 006: the strategy frameworks eligible for confidence-gated auto-approval.
# SWOT reconcile/seed/challenge/stale are deliberately excluded — those stay
# human-vetted (they assert or retire core beliefs).
AUTO_APPROVE_FRAMEWORKS = ("tows", "porter", "pestle", "ansoff", "bcg",
                           "four_corners", "seven_s")
DEFAULT_AUTO_APPROVE_CONF = 0.70


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _load(bucket: str, key: str, s3: Any) -> dict[str, Any]:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body.decode("utf-8"))
    except Exception:  # pragma: no cover - absent store
        return {}


def _save(bucket: str, key: str, obj: dict[str, Any], s3: Any) -> None:
    s3.put_object(
        Bucket=bucket, Key=key,
        Body=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )


def _swot_stores() -> list[str]:
    from src.synth import (ansoff, bcg, four_corners, pestle, porter,
                           seven_s, swot_maintenance, swot_reconcile, swot_seed, tows)

    return [swot_reconcile.PROPOSALS_KEY, swot_seed.SEED_PROPOSALS_KEY,
            swot_maintenance.MAINTENANCE_PROPOSALS_KEY, tows.TOWS_PROPOSALS_KEY,
            porter.PORTER_PROPOSALS_KEY, pestle.PESTLE_PROPOSALS_KEY,
            ansoff.ANSOFF_PROPOSALS_KEY, bcg.BCG_PROPOSALS_KEY,
            four_corners.FOUR_CORNERS_PROPOSALS_KEY, seven_s.SEVEN_S_PROPOSALS_KEY]


def _graph_stores() -> list[str]:
    from src.synth import operatives, relational

    return [relational.RELATIONAL_PROPOSALS_KEY, operatives.PERSON_PROPOSALS_KEY]


def _find(bucket: str, keys: list[str], proposal_id: str, s3: Any):
    """Locate a proposal by id across `keys`. Returns (store_key, store, index)."""
    for key in keys:
        store = _load(bucket, key, s3)
        for i, p in enumerate(store.get("proposals", [])):
            if p.get("id") == proposal_id:
                return key, store, i
    return None, None, None


def _curated_bullet(p: dict[str, Any], *, at: str) -> dict[str, Any]:
    """Turn an approved new/seed/tows proposal into a durable curated bullet record."""
    bullet = {
        "id": p["id"],
        "entity": p.get("entity"),
        "label": p.get("label"),
        "dimension": p.get("dimension"),
        "text": p.get("text"),
        "source_key": p.get("source_key") or p.get("id"),
        "evidence": p.get("evidence") or ([p["narrative_id"]] if p.get("narrative_id") else []),
        "origin": p.get("kind"),
        "date": p.get("date"),
        "approved_at": at,
    }
    fw = p.get("framework")
    if fw and fw != swot_store.DEFAULT_FRAMEWORK:
        bullet["framework"] = fw
    return bullet


def _retire(retirements: list[dict[str, Any]], p: dict[str, Any], at: str) -> str:
    tgt = p.get("target_bullet_id")
    if tgt and not any(r.get("target_bullet_id") == tgt for r in retirements):
        retirements.append({"target_bullet_id": tgt, "entity": p.get("entity"),
                            "proposal_id": p["id"], "approved_at": at})
        return "retirement"
    return "none"


def _reaffirm(bullets: list[dict[str, Any]], p: dict[str, Any], at: str) -> str:
    """Reset a curated bullet's staleness clock (analyst kept it on re-review)."""
    tgt = p.get("target_bullet_id")
    for b in bullets:
        if b.get("id") == tgt:
            b["reaffirmed_at"] = at
            return "reaffirm"
    return "none"


def _apply_curated(bucket: str, p: dict[str, Any], decision: str, s3: Any) -> str:
    """Fold a vetted SWOT decision into swot/curated.json (idempotent). Returns the
    effect ("bullet" | "retirement" | "reaffirm" | "none"). Writes only on a change.

    approved new/seed -> active curated bullet; approved challenge/stale -> retire the
    target; rejected stale -> re-affirm the target (reset its staleness clock). A plain
    new/seed/challenge rejection has no curated effect.
    """
    kind = p.get("kind")
    cur = _load(bucket, swot_store.CURATED_KEY, s3)
    bullets = cur.get("bullets", [])
    retirements = cur.get("retirements", [])
    at = _now()
    if decision == "approved":
        if kind in ("challenge", "stale"):
            effect = _retire(retirements, p, at)
        elif not any(b.get("id") == p["id"] for b in bullets):  # new | seed
            bullets.append(_curated_bullet(p, at=at))
            effect = "bullet"
        else:
            effect = "none"
    elif kind == "stale":  # rejected stale == "keep it" -> re-affirm
        effect = _reaffirm(bullets, p, at)
    else:
        effect = "none"
    if effect != "none":
        _save(bucket, swot_store.CURATED_KEY,
              {"generated_at": at, "bullets": bullets, "retirements": retirements}, s3)
    return effect


def _decide(bucket: str, keys: list[str], proposal_id: str, decision: str, s3: Any,
            *, apply_fn=None) -> dict[str, Any]:
    """Shared idempotent status flip. Optionally apply a decision side effect."""
    if decision not in DECISIONS:
        return {"status": "error", "detail": "decision must be approved|rejected"}
    key, store, i = _find(bucket, keys, proposal_id, s3)
    if store is None:
        return {"status": "noop", "detail": "missing"}
    p = store["proposals"][i]
    prior = p.get("status", "pending")
    if prior in DECISIONS:  # already decided — never double-apply
        return {"status": "noop", "detail": "already decided", "decision": prior,
                "proposal_id": proposal_id}
    effect = apply_fn(bucket, p, decision, s3) if apply_fn is not None else "none"
    p["status"] = decision
    p["decided_at"] = _now()
    _save(bucket, key, store, s3)
    return {"status": decision, "proposal_id": proposal_id, "kind": p.get("kind"),
            "entity": p.get("entity"), "effect": effect}


def vet_swot(bucket: str, proposal_id: str, decision: str, *, s3: Any) -> dict[str, Any]:
    """Approve (promote to curated) or reject a SWOT reconcile/seed proposal."""
    return _decide(bucket, _swot_stores(), proposal_id, decision, s3, apply_fn=_apply_curated)


def vet_graph(bucket: str, proposal_id: str, decision: str, *, s3: Any) -> dict[str, Any]:
    """Approve or reject a relationship-graph (relational/person) proposal."""
    return _decide(bucket, _graph_stores(), proposal_id, decision, s3)


def vet(bucket: str, proposal_id: str, decision: str, *, queue: str, s3: Any) -> dict[str, Any]:
    """Dispatch by queue: 'swot' | 'graph'. Unknown queue -> error."""
    if queue == "swot":
        return vet_swot(bucket, proposal_id, decision, s3=s3)
    if queue == "graph":
        return vet_graph(bucket, proposal_id, decision, s3=s3)
    return {"status": "error", "detail": f"unknown queue: {queue}"}


# --- ADR 006: confidence-gated auto-approval of framework proposals ----------
def _framework_stores() -> dict[str, str]:
    """Map each strategy framework to its proposals.json S3 key."""
    from src.synth import (ansoff, bcg, four_corners, pestle, porter,
                           seven_s, tows)

    return {
        "tows": tows.TOWS_PROPOSALS_KEY,
        "porter": porter.PORTER_PROPOSALS_KEY,
        "pestle": pestle.PESTLE_PROPOSALS_KEY,
        "ansoff": ansoff.ANSOFF_PROPOSALS_KEY,
        "bcg": bcg.BCG_PROPOSALS_KEY,
        "four_corners": four_corners.FOUR_CORNERS_PROPOSALS_KEY,
        "seven_s": seven_s.SEVEN_S_PROPOSALS_KEY,
    }


def _as_confidence(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_threshold(value: Any, *, default: float = DEFAULT_AUTO_APPROVE_CONF) -> float:
    """Coerce a threshold input parameter to a 0..1 confidence. Accepts a fraction
    (0.7) or a percentage (70); None/garbage falls back to `default`."""
    if value is None or value == "":
        return default
    try:
        t = float(value)
    except (TypeError, ValueError):
        return default
    if t > 1.0:  # given as a percentage, e.g. 70 -> 0.70
        t = t / 100.0
    return max(0.0, min(1.0, t))


def auto_approve_frameworks(
    bucket: str, *, threshold: float, s3: Any,
    frameworks: tuple[str, ...] = AUTO_APPROVE_FRAMEWORKS,
) -> dict[str, Any]:
    """Approve every PENDING framework proposal whose confidence >= threshold,
    promoting each into swot/curated.json via the SAME path the vetting UI uses
    (`_apply_curated`). Idempotent: proposals already decided (by a human or a
    prior auto pass) are left untouched. Only the strategy frameworks are eligible
    — SWOT reconcile/seed/challenge/stale are never auto-approved."""
    stores = _framework_stores()
    scanned = approved = promoted = 0
    by_framework: dict[str, int] = {}
    for fw in frameworks:
        key = stores.get(fw)
        if not key:
            continue
        store = _load(bucket, key, s3)
        changed = False
        for p in store.get("proposals", []):
            if p.get("status") != "pending":
                continue
            scanned += 1
            if _as_confidence(p.get("confidence")) < threshold:
                continue
            effect = _apply_curated(bucket, p, "approved", s3)
            p["status"] = "approved"
            p["decided_at"] = _now()
            p["auto_approved"] = True
            p["auto_approved_conf"] = threshold
            approved += 1
            by_framework[fw] = by_framework.get(fw, 0) + 1
            if effect == "bullet":
                promoted += 1
            changed = True
        if changed:
            _save(bucket, key, store, s3)
    return {"threshold": threshold, "scanned": scanned, "approved": approved,
            "promoted": promoted, "by_framework": by_framework}


def lambda_handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    """Pipeline step: auto-approve high-confidence framework proposals.

    Threshold is an input parameter (default 0.70 / 70%): an execution/event
    payload key (`threshold` or `autoapprove_threshold`, a fraction 0.7 or a
    percentage 70) overrides env `ONCA_AUTOAPPROVE_CONF`. Disable the whole step
    with `ONCA_AUTOAPPROVE_ENABLED=0`.
    """
    import boto3

    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}
    if os.environ.get("ONCA_AUTOAPPROVE_ENABLED", "1") in ("0", "false", "False"):
        return {"statusCode": 200, "body": json.dumps({"status": "disabled"})}

    event = event or {}
    raw = event.get("threshold", event.get("autoapprove_threshold"))
    if raw is None:
        raw = os.environ.get("ONCA_AUTOAPPROVE_CONF")
    threshold = normalize_threshold(raw)

    result = auto_approve_frameworks(bucket, threshold=threshold, s3=boto3.client("s3"))
    return {"statusCode": 200, "body": json.dumps({"status": "ok", **result})}
