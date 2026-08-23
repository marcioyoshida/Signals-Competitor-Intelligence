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
from typing import Any

from src.synth import swot_store

DECISIONS = ("approved", "rejected")


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
