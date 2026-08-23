"""Review-queue write endpoint (ADR step 5) — approve/reject a pending proposal.

Fronted by CloudFront: the same basic-auth CloudFront Function that gates the
dashboard also gates this behavior, so the browser's existing Authorization
header authorizes the POST (same origin). The Function URL itself is AuthType
NONE, so a shared origin secret (injected by CloudFront as a custom header,
absent on direct callers) blocks bypassing the edge. Best-effort feed rebuild
republishes feed.json so the panel reflects the decision.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any


def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _rebuild_feed() -> bool:
    """Best-effort async feed rebuild so a decided item drops from the panel."""
    fb = os.environ.get("ONCA_FEED_BUILDER_NAME")
    if not fb:
        return False
    try:
        import boto3

        boto3.client("lambda").invoke(FunctionName=fb, InvocationType="Event", Payload=b"{}")
        return True
    except Exception as exc:  # pragma: no cover - rebuild is best-effort
        print(f"Warning: feed rebuild invoke failed: {exc}")
        return False


def _vet_proposal(payload: dict[str, Any]) -> dict[str, Any]:
    """Approve/reject a queued SWOT or graph proposal (ADR 004 Phase C)."""
    proposal_id = payload.get("proposal_id")
    decision = payload.get("decision")
    queue = payload.get("queue")
    if not proposal_id or decision not in ("approved", "rejected"):
        return _resp(400, {"error": "proposal_id and decision (approved|rejected) required"})
    if queue not in ("swot", "graph"):
        return _resp(400, {"error": "queue must be 'swot' or 'graph'"})

    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not bucket:
        return _resp(500, {"error": "no digests bucket configured"})

    import boto3

    from src.synth import curate

    result = curate.vet(bucket, proposal_id, decision, queue=queue, s3=boto3.client("s3"))
    if result.get("status") == "error":
        return _resp(400, result)
    if result.get("status") == "noop":
        return _resp(409, result)
    result["feed_rebuild"] = _rebuild_feed()
    return _resp(200, result)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    # Origin secret: present only when the request came through CloudFront.
    secret = os.environ.get("ONCA_ORIGIN_SECRET")
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    if secret and headers.get("x-onca-origin") != secret:
        return _resp(403, {"error": "forbidden"})

    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return _resp(400, {"error": "invalid JSON body"})

    # ADR 004 Phase C: vet a queued proposal (SWOT reconcile/seed or graph). Distinct
    # from the entity-registry review queue below (that's DynamoDB REVIEW# items; these
    # are the S3 proposal stores). Approving a SWOT proposal promotes it into the
    # durable curated-belief store; rejecting suppresses it. Idempotent.
    if payload.get("proposal_id"):
        return _vet_proposal(payload)

    review_id = payload.get("review_id")
    decision = payload.get("decision")
    if not review_id or decision not in ("approved", "rejected"):
        return _resp(400, {"error": "review_id and decision (approved|rejected) required"})

    # Decision-time input for reviews whose approval needs a choice (industry:
    # the curator-picked module slugs). Ignored by kinds that don't use it.
    extra: dict[str, Any] = {}
    inds = payload.get("industries")
    if isinstance(inds, list):
        extra["industries"] = [str(i) for i in inds if str(i).strip()]
    if decision == "approved" and payload.get("kind") == "industry" and not extra.get("industries"):
        return _resp(400, {"error": "industry approval requires industries[]"})

    from src.synth import entity_registry

    item = entity_registry.resolve_review(review_id, decision, payload=extra or None)
    if item is None:
        return _resp(409, {"status": "noop", "detail": "missing or already decided"})

    # Republish feed.json (async) so the read-only panel drops the decided item.
    return _resp(200, {"status": decision, "review_id": review_id,
                       "feed_rebuild": _rebuild_feed()})
