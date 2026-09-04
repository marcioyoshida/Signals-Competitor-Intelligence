"""Write-capable Agent API — `POST /api/act` (ADR 020, Phase 1).

The read half of the Agent API (`/api/ask`, ADR 010) answers questions; this is the
**write** half — a thin, authorized, *audited* orchestration layer over the safe
mutation primitives we already trust. Phase 1 proves the **write contract** with NO
officers yet (those are Phase 2): a typed, allow-listed action catalog, a tier-authz
gate, per-request idempotency, a propose-vs-apply classifier, and an append-only
journal entry for every call.

Design (ADR 020 §1):
  * **Authorization** — fronted by CloudFront like the other operator endpoints (basic-
    auth edge + shared origin secret; the Function URL itself is AuthType NONE). Writes
    require an *elevated* capability and are **fail-closed**. The origin-secret operator
    (no JWT) is the elevated legacy actor — consistent with `review_action`/`run_trigger`;
    a JWT identity must carry an elevated tier/group.
  * **Typed catalog** — an intent can only be one of a fixed allowlist (`_CATALOG`); an
    arbitrary-mutation surface never exists by construction. Each entry declares its
    execution class (`apply` | `propose`) and a validated handler.
  * **Two execution classes** — `apply` is reversible/idempotent/low-blast (trigger a run,
    resolve an already-queued proposal, roll a field back). `propose` writes a review-queue
    proposal for anything high-stakes; a human/elevated tier promotes it.
  * **Idempotency** — one `idempotency_key` per request; a replay returns the stored result.
  * **Audit** — every call is journaled to `OncaCurationLog` (ADR 018) with actor, args,
    and outcome (`applied` | `proposed` | `blocked` | `noop`).
  * **No fabrication** — Phase 1 dispatch is deterministic (no LLM); args are typed/validated.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Callable

from src.dashboard import auth
from src.synth import entity_registry

# Tier/group claims that carry the elevated write capability (ADR 002 tiers).
_ELEVATED_TIERS = {"operator", "sovereign"}
_ELEVATED_GROUPS = {"operator", "admin", "sovereign"}

APPLY = "apply"
PROPOSE = "propose"

# The synthetic entity id under which non-entity actions (a pipeline run) are journaled,
# so every act lands in the same append-only audit substrate.
_ACT_SUBJECT = "__act__"


def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _body(event: dict[str, Any]) -> dict[str, Any] | None:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception:
            return None
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def _authorize(event: dict[str, Any]) -> tuple[str, bool]:
    """Return (actor, elevated). The origin-secret operator (no JWT) is elevated by
    design — the same trust the other operator endpoints assume. A JWT identity is
    elevated only if it carries an elevated tier or group claim."""
    identity = auth.identity_from_event(event)
    if identity is None:
        return "operator", True  # origin-secret already verified in the handler
    elevated = (identity.tier in _ELEVATED_TIERS) or bool(
        _ELEVATED_GROUPS.intersection(identity.groups)
    )
    return (identity.sub or "unknown"), elevated


# ---- idempotency (registry table, pk=ACT#<key>) ------------------------------------
def _act_key(idempotency_key: str) -> dict[str, str]:
    return {"pk": f"ACT#{idempotency_key}"}


def _get_act(idempotency_key: str, table: Any | None = None) -> dict[str, Any] | None:
    """Return the stored {status, result} for a prior request, or None. The result is
    kept as a JSON string so DynamoDB never coerces numbers to Decimal (which would
    break json.dumps on replay)."""
    try:
        item = entity_registry._table(table).get_item(Key=_act_key(idempotency_key)).get("Item")
    except Exception:
        return None
    if not item:
        return None
    try:
        return {"status": int(item.get("status") or 200),
                "result": json.loads(item.get("result_json") or "{}")}
    except (ValueError, TypeError):
        return None


def _put_act(idempotency_key: str, status: int, result: dict[str, Any],
             table: Any | None = None) -> None:
    try:
        entity_registry._table(table).put_item(
            Item={**_act_key(idempotency_key), "type": "act", "status": int(status),
                  "result_json": json.dumps(result, ensure_ascii=False),
                  "created_at": entity_registry._now_iso()}
        )
    except Exception as exc:  # pragma: no cover - best-effort, never blocks the response
        print(f"Warning: act idempotency store failed for {idempotency_key}: {exc}")


def _journal(subject: str, intent: str, actor: str, detail: dict[str, Any]) -> None:
    """Append the act to OncaCurationLog (ADR 018). Best-effort — never blocks."""
    entity_registry._log(subject or _ACT_SUBJECT, f"act:{intent}", actor, detail)


# ---- action handlers ---------------------------------------------------------------
# Each returns (outcome, http_status, detail). outcome ∈ applied|proposed|blocked|noop.

def _act_trigger_run(args: dict[str, Any], actor: str) -> tuple[str, int, dict[str, Any]]:
    """apply: schedule ONE debounced pipeline run at the top of the next hour."""
    pipeline_arn = os.environ.get("ONCA_PIPELINE_ARN")
    role_arn = os.environ.get("ONCA_SCHEDULER_ROLE_ARN")
    name = os.environ.get("ONCA_SCHEDULE_NAME", "onca-adhoc-run")
    if not pipeline_arn or not role_arn:
        return "blocked", 500, {"error": "trigger not configured"}
    from src.dashboard import run_trigger

    at = run_trigger.next_top_of_hour()
    params = {
        "Name": name,
        "ScheduleExpression": f"at({at})",
        "ScheduleExpressionTimezone": "America/Sao_Paulo",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {"Arn": pipeline_arn, "RoleArn": role_arn, "Input": "{}"},
        "ActionAfterCompletion": "DELETE",
        "State": "ENABLED",
        "Description": "Onca ad-hoc pipeline run (act, debounced, self-deleting).",
    }
    import boto3

    sch = boto3.client("scheduler")
    try:
        sch.create_schedule(**params)
        created = True
    except sch.exceptions.ConflictException:
        sch.update_schedule(**params)
        created = False
    return "applied", 200, {"at": at, "created": created, "debounced": not created}


def _act_resolve_review(args: dict[str, Any], actor: str) -> tuple[str, int, dict[str, Any]]:
    """apply: promote (approve) or reject an already-queued review proposal. The
    proposal was queued by a discovery/coverage path; this is the elevated decision."""
    review_id = str(args.get("review_id") or "").strip()
    decision = args.get("decision")
    if not review_id or decision not in ("approved", "rejected"):
        return "blocked", 400, {"error": "review_id and decision (approved|rejected) required"}
    extra: dict[str, Any] = {}
    inds = args.get("industries")
    if isinstance(inds, list):
        extra["industries"] = [str(i) for i in inds if str(i).strip()]
    item = entity_registry.resolve_review(review_id, decision, payload=extra or None)
    if item is None:
        return "noop", 409, {"detail": "missing or already decided", "review_id": review_id}
    return "applied", 200, {"review_id": review_id, "decision": decision,
                            "kind": item.get("kind")}


def _act_rollback_field(args: dict[str, Any], actor: str) -> tuple[str, int, dict[str, Any]]:
    """apply: restore a rollback-supported field (industries|parent) to its value just
    before a timestamp — reversible by construction (a curated write over the journal)."""
    entity_id = str(args.get("entity_id") or "").strip()
    field = str(args.get("field") or "").strip()
    before_ts = str(args.get("before_ts") or "").strip()
    if not entity_id or not field or not before_ts:
        return "blocked", 400, {"error": "entity_id, field and before_ts required"}
    try:
        ok = entity_registry.rollback_field(entity_id, field, before_ts)
    except ValueError as exc:
        return "blocked", 400, {"error": str(exc)}
    if not ok:
        return "noop", 409, {"detail": "no prior value to restore", "entity_id": entity_id}
    return "applied", 200, {"entity_id": entity_id, "field": field, "before_ts": before_ts}


def _act_revert_entity(args: dict[str, Any], actor: str) -> tuple[str, int, dict[str, Any]]:
    """apply: roll back every rollback-supported field an entity changed at/after a
    timestamp — undo a bad run in one shot."""
    entity_id = str(args.get("entity_id") or "").strip()
    since_ts = str(args.get("since_ts") or "").strip()
    if not entity_id or not since_ts:
        return "blocked", 400, {"error": "entity_id and since_ts required"}
    reverted = entity_registry.revert_entity_since(entity_id, since_ts)
    if not reverted:
        return "noop", 409, {"detail": "nothing to revert", "entity_id": entity_id}
    return "applied", 200, {"entity_id": entity_id, "reverted": reverted}


def _act_propose_registry_change(
    args: dict[str, Any], actor: str
) -> tuple[str, int, dict[str, Any]]:
    """propose: the SAFE way to request a high-stakes registry change — it never mutates
    the registry, it queues a review-queue proposal for a human/elevated tier to promote.
    This is the target an officer (Phase 2) emits for an industry/parent change."""
    entity_id = str(args.get("entity_id") or "").strip()
    field = str(args.get("field") or "").strip()
    if not entity_id or field not in ("industries", "parent"):
        return "blocked", 400, {"error": "entity_id and field (industries|parent) required"}
    value = args.get("value")
    reason = str(args.get("reason") or "act: proposed registry change").strip()
    rid = entity_registry.propose_review(
        kind="act_registry",
        key=f"{entity_id}:{field}",
        entity_id=entity_id,
        proposed=json.dumps(value, ensure_ascii=False) if value is not None else None,
        reason=reason,
        hint=f"field={field} by={actor}",
        confidence="curated",
        payload={"entity_id": entity_id, "field": field, "value": value, "actor": actor},
    )
    if rid is None:
        return "noop", 409, {"detail": "already proposed", "entity_id": entity_id, "field": field}
    return "proposed", 202, {"review_id": rid, "entity_id": entity_id, "field": field}


# intent -> (execution class, handler, journal-subject key in args)
_CATALOG: dict[str, tuple[str, Callable[..., tuple[str, int, dict[str, Any]]], str | None]] = {
    "trigger_run": (APPLY, _act_trigger_run, None),
    "resolve_review": (APPLY, _act_resolve_review, None),
    "rollback_field": (APPLY, _act_rollback_field, "entity_id"),
    "revert_entity": (APPLY, _act_revert_entity, "entity_id"),
    "propose_registry_change": (PROPOSE, _act_propose_registry_change, "entity_id"),
}


def catalog() -> dict[str, str]:
    """Public action catalog {intent: execution_class} — the fixed mutation surface."""
    return {intent: cls for intent, (cls, _h, _s) in _CATALOG.items()}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    # Origin secret: present only when the request came through CloudFront (the edge
    # basic-auth already ran). Absent on direct callers → blocked.
    secret = os.environ.get("ONCA_ORIGIN_SECRET")
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    if secret and headers.get("x-onca-origin") != secret:
        return _resp(403, {"error": "forbidden"})

    # GET → advertise the catalog (read-only; helps officers/clients discover intents).
    method = str(((event.get("requestContext") or {}).get("http") or {}).get("method") or "POST")
    if method.upper() == "GET":
        return _resp(200, {"catalog": catalog()})

    body = _body(event)
    if body is None:
        return _resp(400, {"error": "invalid JSON body"})

    actor, elevated = _authorize(event)
    if not elevated:
        return _resp(403, {"error": "write requires an elevated capability"})

    intent = str(body.get("intent") or "").strip()
    entry = _CATALOG.get(intent)
    if entry is None:
        return _resp(400, {"error": "unknown intent", "catalog": list(_CATALOG)})
    exec_class, handler, subject_key = entry
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    idem = str(body.get("idempotency_key") or "").strip()

    # Idempotent replay: a prior result for this key wins (no double-application).
    if idem:
        prior = _get_act(idem)
        if prior is not None:
            return _resp(prior["status"], {**prior["result"], "idempotent_replay": True})

    outcome, status, detail = handler(args, actor)
    subject = str(args.get(subject_key)) if subject_key and args.get(subject_key) else _ACT_SUBJECT

    result = {
        "intent": intent,
        "execution_class": exec_class,
        "outcome": outcome,
        "actor": actor,
        "request_id": getattr(context, "aws_request_id", None),
        **detail,
    }
    # Journal every call (applied|proposed|blocked|noop) to OncaCurationLog.
    _journal(subject, intent, actor, {
        "outcome": outcome, "execution_class": exec_class, "args": args,
        "request_id": result["request_id"], "idempotency_key": idem or None,
    })
    if idem:
        _put_act(idem, status, result)
    return _resp(status, result)
