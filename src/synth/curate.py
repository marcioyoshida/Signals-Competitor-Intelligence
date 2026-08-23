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
    from src.synth import swot_reconcile, swot_seed

    return [swot_reconcile.PROPOSALS_KEY, swot_seed.SEED_PROPOSALS_KEY]


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
    """Turn an approved new/seed proposal into a durable curated bullet record."""
    return {
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


def _apply_curated(bucket: str, p: dict[str, Any], s3: Any) -> str:
    """Fold an approved SWOT proposal into swot/curated.json (idempotent by id).

    new/seed -> an active curated bullet; challenge -> a retirement of the target.
    Returns the effect applied ("bullet" | "retirement" | "none").
    """
    cur = _load(bucket, swot_store.CURATED_KEY, s3)
    bullets = cur.get("bullets", [])
    retirements = cur.get("retirements", [])
    at = _now()
    effect = "none"
    if p.get("kind") == "challenge":
        tgt = p.get("target_bullet_id")
        if tgt and not any(r.get("target_bullet_id") == tgt for r in retirements):
            retirements.append({"target_bullet_id": tgt, "entity": p.get("entity"),
                                "proposal_id": p["id"], "approved_at": at})
            effect = "retirement"
    else:  # new | seed
        if not any(b.get("id") == p["id"] for b in bullets):
            bullets.append(_curated_bullet(p, at=at))
            effect = "bullet"
    _save(bucket, swot_store.CURATED_KEY,
          {"generated_at": at, "bullets": bullets, "retirements": retirements}, s3)
    return effect


def _decide(bucket: str, keys: list[str], proposal_id: str, decision: str, s3: Any,
            *, apply_fn=None) -> dict[str, Any]:
    """Shared idempotent status flip. Optionally apply an approval side effect."""
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
    effect = "none"
    if decision == "approved" and apply_fn is not None:
        effect = apply_fn(bucket, p, s3)
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
