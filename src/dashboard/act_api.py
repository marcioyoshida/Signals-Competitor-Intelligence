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

from src.dashboard import auth, officers
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


# ---- Phase 2 officer actions (each backed by an existing safe primitive) -----------
def _act_open_watch(args: dict[str, Any], actor: str) -> tuple[str, int, dict[str, Any]]:
    """apply: open a durable watch on an entity/segment/instrument (Strategic/Regulator).
    Additive, reversible (a WATCH# item), low-blast — the auto-apply safe class."""
    target = str(args.get("target") or "").strip()
    if not target:
        return "blocked", 400, {"error": "target required"}
    kind = str(args.get("kind") or "watch").strip()
    note = str(args.get("note") or "").strip()
    pk = f"WATCH#{kind}#{entity_registry.normalize_alias(target)}"
    try:
        entity_registry._table().put_item(Item={
            "pk": pk, "type": "watch", "target": target, "kind": kind, "note": note,
            "by": actor, "created_at": entity_registry._now_iso(),
        })
    except Exception as exc:  # pragma: no cover - store best-effort
        return "blocked", 500, {"error": f"watch store failed: {exc}"}
    return "applied", 200, {"watch": pk, "target": target, "kind": kind}


def _act_run_integrity_audit(args: dict[str, Any], actor: str) -> tuple[str, int, dict[str, Any]]:
    """apply (read-only): run the integrity detectors over the live feed + registry and
    return the finding counts (Compliance). No mutation — a diagnostic."""
    from src.synth import integrity

    bucket = os.environ.get("ONCA_SITE_BUCKET")
    feed: dict[str, Any] = {}
    if bucket:
        try:
            import boto3

            obj = boto3.client("s3").get_object(Bucket=bucket, Key="feed.json")
            feed = json.loads(obj["Body"].read())
        except Exception as exc:  # pragma: no cover - feed best-effort
            print(f"Warning: integrity audit could not load feed.json: {exc}")
    report = integrity.audit(feed, entity_registry.list_entities())
    top = [f for f in report.get("findings", []) if f.get("severity") == "high"][:10]
    return "applied", 200, {"total": report.get("total"), "counts": report.get("counts"),
                            "high": top}


def _act_flag_entity(args: dict[str, Any], actor: str) -> tuple[str, int, dict[str, Any]]:
    """propose: flag a sanction/distress/integrity hit on an entity for human review
    (Compliance). Queues a review — never mutates the entity."""
    entity_id = str(args.get("entity_id") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if not entity_id or not reason:
        return "blocked", 400, {"error": "entity_id and reason required"}
    rid = entity_registry.propose_review(
        kind="compliance_flag", key=entity_id, entity_id=entity_id,
        reason=reason, hint=f"flag by={actor}", confidence="curated",
        payload={"entity_id": entity_id, "reason": reason, "actor": actor,
                 "risk": args.get("risk")},
    )
    if rid is None:
        return "noop", 409, {"detail": "already flagged", "entity_id": entity_id}
    return "proposed", 202, {"review_id": rid, "entity_id": entity_id}


def _act_curate_belief(args: dict[str, Any], actor: str) -> tuple[str, int, dict[str, Any]]:
    """propose: propose a competitive-belief bullet for review (Strategic). Never writes
    the belief store directly — the ADR-004 vetting path promotes it."""
    entity_id = str(args.get("entity_id") or "").strip()
    bullet = str(args.get("bullet") or "").strip()
    axis = str(args.get("axis") or "strength").strip()
    if not entity_id or not bullet:
        return "blocked", 400, {"error": "entity_id and bullet required"}
    rid = entity_registry.propose_review(
        kind="belief_bullet", key=f"{entity_id}:{axis}:{bullet[:40]}", entity_id=entity_id,
        proposed=bullet, reason=f"tese ({axis})", hint=f"axis={axis} by={actor}",
        confidence="fuzzy",
        payload={"entity_id": entity_id, "axis": axis, "bullet": bullet, "actor": actor},
    )
    if rid is None:
        return "noop", 409, {"detail": "already proposed", "entity_id": entity_id}
    return "proposed", 202, {"review_id": rid, "entity_id": entity_id, "axis": axis}


def _act_propose_vertical(args: dict[str, Any], actor: str) -> tuple[str, int, dict[str, Any]]:
    """propose: propose a new industry/vertical for review (Product, ADR-019). Queues a
    review — provisioning a vertical stays a curated decision."""
    name = str(args.get("name") or "").strip()
    rationale = str(args.get("rationale") or "").strip()
    if not name:
        return "blocked", 400, {"error": "name required"}
    rid = entity_registry.propose_review(
        kind="vertical_proposal", key=entity_registry.normalize_alias(name),
        proposed=name, reason=rationale or "nova vertical", hint=f"by={actor}",
        confidence="fuzzy", payload={"name": name, "rationale": rationale, "actor": actor},
    )
    if rid is None:
        return "noop", 409, {"detail": "already proposed", "name": name}
    return "proposed", 202, {"review_id": rid, "name": name}


# ---- ADR 021 §D Step 1: decision capture (append-only, auto-apply) -----------------
def _act_record_decision(args: dict[str, Any], actor: str) -> tuple[str, int, dict[str, Any]]:
    """apply: capture an executive decision on an officer recommendation (append-only). The
    reward-signal label for the ADR-021 metrics + KB flywheel. Low-blast (a new DECISION# item)."""
    from src.synth import decision_log

    try:
        item = decision_log.record_decision(
            officer=str(args.get("officer") or "").strip(),
            recommendation=str(args.get("recommendation") or "").strip(),
            verdict=str(args.get("verdict") or "").strip(),
            actor=actor,
            industry=(args.get("industry") or None),
            action_ref=(args.get("action_ref") or None),
            evidence_id=(args.get("evidence_id") or None),
            context_id=(args.get("context_id") or None),
            rationale=(args.get("rationale") or None),
        )
    except ValueError as exc:
        return "blocked", 400, {"error": str(exc)}
    return "applied", 200, {"decision_id": item["decision_id"], "verdict": item["verdict"],
                            "officer": item.get("officer"), "industry": item.get("industry")}


def _act_set_outcome(args: dict[str, Any], actor: str) -> tuple[str, int, dict[str, Any]]:
    """apply: stamp the realized outcome on a captured decision (the §E/§F label)."""
    from src.synth import decision_log

    did = str(args.get("decision_id") or "").strip()
    if not did:
        return "blocked", 400, {"error": "decision_id required"}
    try:
        item = decision_log.set_outcome(did, str(args.get("outcome") or "").strip(),
                                        actor=actor, note=args.get("note"))
    except ValueError as exc:
        return "blocked", 400, {"error": str(exc)}
    if item is None:
        return "noop", 404, {"detail": "decision not found", "decision_id": did}
    # NB: not "outcome" — that key is the top-level result verb (applied|noop|…).
    return "applied", 200, {"decision_id": did, "decision_outcome": item["outcome"]}


def _act_append_reference(args: dict[str, Any], actor: str) -> tuple[str, int, dict[str, Any]]:
    """apply: the §H CORS-beacon target — append a consulted first-party source link to a
    decision's evidence trail (feeds the decision's KB precedent). Best-effort, no PII."""
    from src.synth import decision_log

    did = str(args.get("decision_id") or "").strip()
    url = str(args.get("url") or "").strip()
    if not did or not url:
        return "blocked", 400, {"error": "decision_id and url required"}
    ok = decision_log.append_reference(did, url, officer=args.get("officer"))
    if not ok:
        return "noop", 404, {"detail": "decision not found or duplicate url", "decision_id": did}
    return "applied", 200, {"decision_id": did, "url": url}


def _act_record_engagement(args: dict[str, Any], actor: str) -> tuple[str, int, dict[str, Any]]:
    """apply (telemetry): capture an executive attention event — a headline expand/open (§E
    Engagement). Append-only, NOT journaled (high-frequency). Best-effort."""
    from src.synth import engagement_log

    try:
        item = engagement_log.record_engagement(
            kind=str(args.get("kind") or "headline"), actor=actor,
            officer=args.get("officer"), sector=args.get("sector"),
            card_id=args.get("card_id"), entity=args.get("entity"),
            action=args.get("action"), threat_score=args.get("threat_score"),
            industries=args.get("industries") if isinstance(args.get("industries"), list) else None,
            topics=args.get("topics") if isinstance(args.get("topics"), list) else None)
    except Exception as exc:  # pragma: no cover - telemetry best-effort
        return "noop", 200, {"detail": f"engagement skipped: {exc}"}
    return "applied", 200, {"engagement_id": item["engagement_id"], "kind": item["kind"],
                            "action": item["action"]}


# Intents whose calls are NOT written to the OncaCurationLog audit journal (high-frequency
# telemetry, not a state mutation).
_NO_JOURNAL = frozenset({"record_engagement"})


# intent -> (execution class, handler, journal-subject key in args)
_CATALOG: dict[str, tuple[str, Callable[..., tuple[str, int, dict[str, Any]]], str | None]] = {
    "trigger_run": (APPLY, _act_trigger_run, None),
    "resolve_review": (APPLY, _act_resolve_review, None),
    "rollback_field": (APPLY, _act_rollback_field, "entity_id"),
    "revert_entity": (APPLY, _act_revert_entity, "entity_id"),
    "propose_registry_change": (PROPOSE, _act_propose_registry_change, "entity_id"),
    # Phase 2 officer actions
    "open_watch": (APPLY, _act_open_watch, "target"),
    "run_integrity_audit": (APPLY, _act_run_integrity_audit, None),
    "flag_entity": (PROPOSE, _act_flag_entity, "entity_id"),
    "curate_belief": (PROPOSE, _act_curate_belief, "entity_id"),
    "propose_vertical": (PROPOSE, _act_propose_vertical, None),
    # ADR 021 §D Step 1 — decision capture (shared across officers)
    "record_decision": (APPLY, _act_record_decision, None),
    "set_outcome": (APPLY, _act_set_outcome, None),
    # ADR 021 §H — CORS followed-link beacon (shared)
    "append_reference": (APPLY, _act_append_reference, None),
    # ADR 021 §E — engagement telemetry (attention signal)
    "record_engagement": (APPLY, _act_record_engagement, None),
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

    # GET → advertise the catalog + officer roster (helps clients discover intents/officers).
    method = str(((event.get("requestContext") or {}).get("http") or {}).get("method") or "POST")
    if method.upper() == "GET":
        return _resp(200, {"catalog": catalog(), "officers": officers.roster()})

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

    # Phase 2/3: officer scoping + chief-of-staff hand-off. An `officer` may only emit its
    # own catalog actions; an action owned EXCLUSIVELY by another officer is handed off to
    # that owner (journaled), not rejected. With no `officer`, an exclusively-owned action
    # auto-routes to its owner (the chief-of-staff dispatch).
    requested_officer = str(body.get("officer") or "").strip() or None
    handoff: dict[str, str] | None = None
    if requested_officer is not None:
        if not officers.is_officer(requested_officer):
            return _resp(400, {"error": "unknown officer", "officers": list(officers.OFFICERS)})
        if intent in officers.catalog(requested_officer):
            effective_officer: str | None = requested_officer
        else:
            owner = officers.owner_of(intent)
            if owner and owner != requested_officer:
                effective_officer = owner
                handoff = {"from": requested_officer, "to": owner}
            else:
                return _resp(403, {"error": "intent not in officer catalog",
                                   "officer": requested_officer,
                                   "allowed": list(officers.catalog(requested_officer))})
    else:
        effective_officer = officers.owner_of(intent)  # auto-route (may be None for shared)

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
        "officer": effective_officer,
        "request_id": getattr(context, "aws_request_id", None),
        **detail,
    }
    if handoff:
        result["handoff"] = handoff
    # Journal every call (applied|proposed|blocked|noop) to OncaCurationLog — except
    # high-frequency telemetry intents (engagement), which have their own append-only store.
    if intent not in _NO_JOURNAL:
        _journal(subject, intent, actor, {
            "outcome": outcome, "execution_class": exec_class, "args": args,
            "officer": effective_officer, "handoff": handoff,
            "request_id": result["request_id"], "idempotency_key": idem or None,
        })
    if idem:
        _put_act(idem, status, result)
    return _resp(status, result)
