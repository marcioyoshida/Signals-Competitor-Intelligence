"""Coverage-gap API (`/api/gaps/`, ADR-014) — the dashboard "Pontos Cegos" surface.

Read the coverage-gap store and, on the "Remediar" button, run the remediation loop
for a single gap: triage → safe auto-fix (bounded registry backfills) → re-verify by
re-asking the agent → resolve, or leave proposed. GitHub-issue creation stays in the
out-of-band pipeline (no `gh`/token in Lambda; `open_issue` no-ops here safely).

Auth mirrors the other `/api/*` Lambdas: the Function URL only trusts requests
carrying the CloudFront-injected origin secret.

Routes (under /api/gaps):
  GET  /            list open + proposed gaps (fresh from the store)
  POST /remediate   {id} run remediation for one gap; returns its new status

Scheduled (#295): the weekly EventBridge rule invokes this function directly with the fixed
input SCHEDULED_EVENT. That runs `coverage.scheduled_pass`: triage, the bounded backfills,
and a capped re-ask of the agent (Nova Lite, feed cards only; owner exception 2026-11-01).
It never opens an issue. A Function URL or API
Gateway request can't produce that event: AWS sets their top-level keys, and the
caller's payload lives in `body`.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any


def _resp(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def _method(event: dict[str, Any]) -> str:
    rc = (event.get("requestContext") or {}).get("http") or {}
    return str(rc.get("method") or event.get("httpMethod") or "GET").upper()


def _path(event: dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "")


def _body(event: dict[str, Any]) -> dict[str, Any] | None:
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        return None


def _verifier(site_bucket: str | None):
    """Re-ask the live agent; True if the question now grounds."""
    if not site_bucket:
        return None
    from src.dashboard import agent_ask
    from src.synth.bedrock_llm import converse

    def verify(q: str) -> bool:
        try:
            feed = agent_ask._load_feed(site_bucket)
            res = agent_ask.answer(
                q, feed=feed, converser=converse,
                kb_retrieve=agent_ask._kb_retrieve if os.environ.get("ONCA_KB_ID") else None,
            )
            return bool(res.get("grounded"))
        except Exception as exc:  # pragma: no cover
            print(f"gap verify failed: {exc}")
            return False
    return verify


SCHEDULED_EVENT = {"coverage_loop": "scheduled"}


def _scheduled(event: Any) -> bool:
    return isinstance(event, dict) and event == SCHEDULED_EVENT


def run_scheduled() -> dict[str, Any]:
    """The weekly pass (#295). Returns its summary; never raises."""
    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not bucket:
        return {"status": "not configured"}
    from src.synth import coverage
    from src.synth.entities import resolve_entities

    verifier, feed = None, None
    site = os.environ.get("ONCA_SITE_BUCKET")
    if site:
        try:
            from src.dashboard import agent_ask

            feed = agent_ask._load_feed(site)
            verifier = coverage.feed_verifier(feed)
        except Exception as exc:  # pragma: no cover - triage still runs without the verify
            print(f"Warning: coverage verify unavailable: {exc}")
    try:
        summary = coverage.scheduled_pass(bucket, resolver=resolve_entities, verifier=verifier,
                                          feed=feed)
    except Exception as exc:  # pragma: no cover
        print(f"coverage scheduled pass failed: {exc}")
        return {"status": "error"}
    print(f"coverage scheduled pass: {json.dumps(summary, ensure_ascii=False, default=str)}")
    return {"status": "ok", **summary}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if _scheduled(event):
        return run_scheduled()
    # Auth (Phase C increment 2): a verified Cognito identity (API Gateway JWT
    # authorizer) OR the legacy CloudFront origin secret.
    from src.dashboard.auth import identity_from_event, origin_secret_ok

    identity = identity_from_event(event)
    # Fail closed on the legacy leg: an unset ONCA_ORIGIN_SECRET used to make
    # `origin_ok` True for everyone, which turned a missing env var into an
    # unauthenticated endpoint. Now only a verified identity or a matching
    # secret gets through.
    if identity is None and not origin_secret_ok(event):
        return _resp(403, {"error": "forbidden"})

    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not bucket:
        return _resp(500, {"error": "not configured"})

    from src.synth import coverage

    method = _method(event)
    path = _path(event)
    try:
        if method == "GET":
            idx = coverage.load_index(bucket)
            gaps = coverage.list_open(idx)
            return _resp(200, {"gaps": gaps, "count": len(gaps)})

        if method == "POST" and path.endswith("/remediate"):
            body = _body(event)
            if body is None:
                return _resp(400, {"error": "invalid JSON body"})
            gid = str(body.get("id") or "").strip()
            if not gid:
                return _resp(400, {"error": "id required"})
            from src.synth.entities import resolve_entities

            feed = None
            if os.environ.get("ONCA_SITE_BUCKET"):
                try:
                    from src.dashboard import agent_ask

                    feed = agent_ask._load_feed(os.environ["ONCA_SITE_BUCKET"])
                except Exception as exc:  # pragma: no cover - keyword triage still works
                    print(f"Warning: feed for triage evidence unavailable: {exc}")
            summary = coverage.remediate(
                bucket,
                resolver=resolve_entities,
                verifier=_verifier(os.environ.get("ONCA_SITE_BUCKET")),
                only_id=gid,
                feed=feed,
            )
            idx = coverage.load_index(bucket)
            rec = (idx.get("records") or {}).get(gid)
            return _resp(200, {"summary": summary, "gap": rec})

        return _resp(404, {"error": "not found"})
    except Exception as exc:  # pragma: no cover - defensive; never leak a stack
        print(f"gaps_api error: {exc}")
        return _resp(500, {"error": "internal error"})
