"""ADR 016 addendum (2026-09-22) Decision 3: the vendor-side `POST /resolve`
endpoint — a Sovereign (marketplace-plane) tenant's ONLY window into the
registry (ADR 005 §2). `src/synth/resolver.py` is the client half of this
same contract; this module is the server half.

**Auth.** An AWS_IAM Lambda Function URL, SigV4-signed by the CALLING
tenant's own `OncaResolveCallerRole` (`infra/tenant_stack.py`) — no shared API
key (Decision 3's explicit rejection: one leak must not compromise every
tenant, and revocation must be a single lever). AWS itself rejects an
unsigned/wrongly-signed request before it ever reaches this handler; this
module's own job is narrower: the caller declares which tenant it claims to
be (`?tenant_id=`, since a bare IAM ARN doesn't self-identify a tenant slug),
and `_authorize` verifies the CALLER'S ACTUAL SIGNED IDENTITY
(`requestContext.authorizer.iam.userArn`, populated by AWS_IAM auth — never
anything the client puts in the body) resolves to the SAME account + role
name as the `resolve_caller_role_arn` registered against that tenant_id in
`tenant_config.py`. A tenant can therefore never resolve as a different
tenant than the one whose role it actually holds. Onboarding a real tenant's
role needs BOTH `lambda:InvokeFunctionUrl` AND `lambda:InvokeFunction` granted
(a function URL created after October 2025 requires both — see
`infra/app.py`'s `OncaResolveApi` block for the live-verified detail; granting
only the first produces a bare AWS-edge 403 that never reaches this handler
and looks exactly like a signature bug). A tenant not
provisioned on the `marketplace` plane (ADR 016 addendum Decision 2) is
refused outright — a SaaS-plane tenant's synth already holds the registry
directly and has no legitimate reason to call this endpoint at all.

**Contract, exactly (ADR 005 §2, unchanged by this addendum):**
  POST /resolve  { name?, cnpj_root?, ispb?, ticker? }
    -> 200 { entity_id, display_name, canonical_id, industries[], confidence }
    -> 404 {}                          (no match — never a fuzzy guess)
    -> 400 / 403 / 429 { error }

Reuses `src/synth/resolver.resolve_known_id` for the actual lookup — the same
single-`get_item` registry-mode code path `entities.py` already uses
internally (this Lambda's environment never sets `ONCA_RESOLUTION_MODE`, so
`resolver.mode()` defaults to `registry`, exactly right for the vendor side).
There is only ONE implementation of "resolve by known id against the
registry" in the repo; this module does not duplicate it.

**No list/scan/batch, ever.** Exactly one identifier field is required per
call — zero is nothing to resolve, more than one is a caller trying to widen
a single call's yield. `entity_registry` is never scanned here, only
single-key lookups, same as every other caller of `resolve_known_id`.

**Leak controls (ADR 005 §2, "inherited"):** a per-tenant per-minute rate
limit (protects availability/cost — a flat, generous cap, not the real
defense) and a per-tenant per-day BREADTH counter of distinct entity_ids
actually resolved, which prints a loud canary warning (never blocks) once a
tenant crosses a threshold consistent with "learning the registry" rather
than "processing today's narrative volume" — forensics, per ADR 005 §2's
"canary records," not an auto-lockout that could take a legitimate tenant
offline on a false positive.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from src.dashboard import tenant_config
from src.synth import resolver

IDENTIFIER_FIELDS = ("name", "cnpj_root", "ispb", "ticker")

# A generous per-tenant cap: normal synth traffic (processing one day's worth
# of narrative volume) is nowhere near this; it exists to bound cost/
# availability impact of a misbehaving client, not as the anti-enumeration
# control (that's the breadth counter below).
RATE_LIMIT_PER_MINUTE = 60

# Distinct entity_ids resolved by one tenant in one day, before a canary
# warning fires. A tenant's own corpus touches at most a few dozen entities a
# day in the normal case; three digits is already well past "processing
# today's news," not a hard technical ceiling.
BREADTH_ANOMALY_THRESHOLD = 200

_ASSUMED_ROLE_RE = re.compile(r"^arn:aws:sts::(\d{12}):assumed-role/([^/]+)/.+$")
_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::(\d{12}):role/(.+)$")


def _resp(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def _method(event: dict[str, Any]) -> str:
    rc = (event.get("requestContext") or {}).get("http") or {}
    return str(rc.get("method") or event.get("httpMethod") or "GET").upper()


def _body(event: dict[str, Any]) -> dict[str, Any] | None:
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        return None


def _caller_arn(event: dict[str, Any]) -> str | None:
    # Populated by AWS itself for an AWS_IAM-auth Lambda Function URL — the
    # request never reaches here at all unless it was validly SigV4-signed,
    # so this is a verified identity, not a client-supplied claim.
    iam = (((event.get("requestContext") or {}).get("authorizer") or {}).get("iam")) or {}
    return str(iam.get("userArn") or "").strip() or None


def _tenant_id(event: dict[str, Any]) -> str | None:
    qs = event.get("queryStringParameters") or {}
    return str(qs.get("tenant_id") or "").strip() or None


def _caller_role_identity(caller_arn: str) -> tuple[str, str] | None:
    m = _ASSUMED_ROLE_RE.match(caller_arn)
    return (m.group(1), m.group(2)) if m else None


def _registered_role_identity(role_arn: str) -> tuple[str, str] | None:
    m = _ROLE_ARN_RE.match(role_arn)
    return (m.group(1), m.group(2)) if m else None


def _authorize(event: dict[str, Any], *, config_table: Any = None) -> tuple[str | None, str | None, str | None]:
    """Return (tenant_id, caller_arn, error)."""
    tenant_id = _tenant_id(event)
    if not tenant_id:
        return None, None, "tenant_id query parameter required"
    caller_arn = _caller_arn(event)
    if not caller_arn:
        return None, None, "no verified caller identity"

    config = tenant_config.get_tenant_config(tenant_id, table=config_table)
    if not config:
        return None, None, "unknown tenant"
    if config.get("plane") != "marketplace":
        # ADR 016 addendum Decision 2: /resolve is the in-account (marketplace-
        # plane) contract. A SaaS-plane tenant already holds the registry
        # directly; reaching this endpoint is a misconfiguration, refused
        # rather than silently served.
        return None, None, "tenant is not provisioned on the marketplace plane"

    registered_arn = str(config.get("resolve_caller_role_arn") or "")
    caller_identity = _caller_role_identity(caller_arn)
    registered_identity = _registered_role_identity(registered_arn)
    if not caller_identity or not registered_identity or caller_identity != registered_identity:
        return None, None, "caller identity does not match the registered resolve-caller role for this tenant"
    return tenant_id, caller_arn, None


def _quota_table(table: Any = None) -> Any:
    if table is not None:
        return table
    import boto3

    return boto3.resource("dynamodb").Table(
        os.environ.get("ONCA_RESOLVE_QUOTA_TABLE", "onca-resolve-quota")
    )


def _rate_limit_ok(tenant_id: str, *, table: Any = None, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    minute_bucket = int(now // 60)
    key = f"{tenant_id}#rate#{minute_bucket}"
    try:
        resp = _quota_table(table).update_item(
            Key={"pk": key},
            UpdateExpression="ADD call_count :one SET ttl = if_not_exists(ttl, :ttl)",
            ExpressionAttributeValues={":one": 1, ":ttl": int(now) + 120},
            ReturnValues="UPDATED_NEW",
        )
    except Exception as exc:  # pragma: no cover - transient
        # Fail OPEN on the counter itself: a quota-table outage must not take
        # down every tenant's resolve traffic — the breadth canary below is
        # the actual anti-enumeration control and doesn't depend on this path.
        print(f"Warning: resolve_api rate-limit check failed, allowing: {exc}")
        return True
    count = int((resp.get("Attributes") or {}).get("call_count") or 0)
    return count <= RATE_LIMIT_PER_MINUTE


def _record_breadth(tenant_id: str, entity_id: str, *, table: Any = None, now: float | None = None) -> None:
    now = now if now is not None else time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    key = f"{tenant_id}#breadth#{day}"
    try:
        resp = _quota_table(table).update_item(
            Key={"pk": key},
            UpdateExpression="ADD entity_ids :e SET ttl = if_not_exists(ttl, :ttl)",
            ExpressionAttributeValues={":e": {entity_id}, ":ttl": int(now) + 172800},
            ReturnValues="UPDATED_NEW",
        )
    except Exception as exc:  # pragma: no cover - transient
        print(f"Warning: resolve_api breadth tracking failed (non-blocking): {exc}")
        return
    breadth = len((resp.get("Attributes") or {}).get("entity_ids") or ())
    if breadth == BREADTH_ANOMALY_THRESHOLD:
        # Fires exactly once per tenant per day (the count only crosses the
        # threshold once) — a forensics canary, per ADR 005 §2, never a block.
        print(
            f"CANARY resolve_api: tenant={tenant_id} crossed {BREADTH_ANOMALY_THRESHOLD} "
            f"distinct entities resolved on {day} — review for bulk-enumeration behavior"
        )


def handle(event: dict[str, Any], *, config_table: Any = None, quota_table: Any = None) -> dict[str, Any]:
    if _method(event) != "POST":
        return _resp(405, {"error": "method not allowed"})

    tenant_id, caller_arn, err = _authorize(event, config_table=config_table)
    if err:
        return _resp(403, {"error": err})

    body = _body(event)
    if body is None:
        return _resp(400, {"error": "invalid JSON body"})

    identifiers = {k: body[k] for k in IDENTIFIER_FIELDS if body.get(k)}
    if len(identifiers) != 1:
        return _resp(400, {"error": "exactly one of name/cnpj_root/ispb/ticker is required"})

    if not _rate_limit_ok(tenant_id, table=quota_table):
        return _resp(429, {"error": "rate limit exceeded"})

    try:
        result = resolver.resolve_known_id(**identifiers)
    except Exception as exc:  # pragma: no cover - defensive; never leak a stack
        print(f"resolve_api error: {exc}")
        return _resp(500, {"error": "internal error"})

    if result is None:
        print(f"resolve_api: MISS tenant={tenant_id} caller={caller_arn} query={identifiers}")
        return _resp(404, {})

    _record_breadth(tenant_id, result["entity_id"], table=quota_table)
    print(f"resolve_api: HIT tenant={tenant_id} caller={caller_arn} entity_id={result['entity_id']}")
    return _resp(200, result)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return handle(event)
