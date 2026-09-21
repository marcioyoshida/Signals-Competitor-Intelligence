"""Stripe checkout webhook — the paid half of the Entry self-serve funnel (#115/E1).

Today the only unprovisioned-identity write path is `tenant_config.self_register_entry_tenant`,
which is FREE: a first-time Google login picks entry-tier industries and gets a tenant. This
module is its paid counterpart — Stripe Checkout completes, Stripe calls here, and the tenant
is entitled without an operator in the loop.

**This endpoint is the one place in Onça where an anonymous, unauthenticated caller can cause
an entitlement to be created.** Everything below follows from that:

1. **The signature IS the authentication.** There is no Cognito JWT here — Stripe is not a
   person and has no identity in our pool. A request whose `Stripe-Signature` does not verify
   against the endpoint's signing secret is indistinguishable from an attacker minting free
   subscriptions, so verification is FAIL-CLOSED: no secret configured ⇒ reject everything.
   An unverifiable webhook must never be "allowed through for now".

2. **Verification is done here, not by the Stripe SDK.** The scheme is documented and tiny
   (HMAC-SHA256 over `"{timestamp}.{raw_body}"`), and the Lambda bundle is hand-staged
   (see build/lambda) — pulling the SDK in for one HMAC would add a dependency to every
   Lambda sharing that asset. `hmac.compare_digest` guards the comparison; a plain `==`
   here would leak the signature a byte at a time.

3. **A replayed old event is rejected** (`tolerance`), because the signature of a legitimate
   past event stays valid forever otherwise.

4. **The customer never chooses what they get.** Modules and tier are derived server-side from
   the Stripe *price id*, never read from the session payload — the client controls the
   checkout request, so trusting it would let anyone request `saas` at the entry price.
   `put_tenant_config`'s `allowed_industries_for_tier` allow-list is a second, independent
   check that an entry price cannot yield a non-entry module.

5. **Stripe retries until it gets a 2xx**, and delivers at-least-once. Provisioning is keyed
   on the Stripe event id so a retry is a no-op rather than a second tenant. Idempotency is
   recorded only AFTER the tenant write succeeds: recording first would turn a transient
   DynamoDB error into a paying customer who never gets access and whom Stripe will never
   retry.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

# Stripe's own default. An event older than this is a replay, not a delivery.
DEFAULT_TOLERANCE_SEC = 300

IDEMPOTENCY_PK = "BILLING#"


class SignatureError(Exception):
    """The request did not come from Stripe (or came too long ago)."""


def parse_signature_header(header: str) -> tuple[int, list[str]]:
    """Split `t=1614...,v1=abc,v1=def` into (timestamp, [v1 signatures]).

    Stripe sends MULTIPLE v1 signatures while a secret is being rotated; accepting
    only the first would break every rotation.
    """
    ts = 0
    sigs: list[str] = []
    for part in str(header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                ts = int(value)
            except ValueError:
                ts = 0
        elif key == "v1" and value:
            sigs.append(value)
    return ts, sigs


def verify_signature(payload: str | bytes, header: str, secret: str | None,
                     *, tolerance: int = DEFAULT_TOLERANCE_SEC,
                     now: float | None = None) -> None:
    """Raise SignatureError unless `payload` was signed by `secret` recently.

    `payload` must be the RAW body exactly as received — re-serializing parsed JSON
    changes key order and whitespace and will never match.
    """
    if not secret:
        # Fail closed. An unconfigured secret is a misconfiguration, not permission.
        raise SignatureError("no signing secret configured")
    ts, sigs = parse_signature_header(header)
    if not ts or not sigs:
        raise SignatureError("malformed Stripe-Signature header")
    age = (now if now is not None else time.time()) - ts
    if abs(age) > tolerance:
        raise SignatureError(f"timestamp outside tolerance ({age:.0f}s)")
    body = payload.encode("utf-8") if isinstance(payload, str) else payload
    signed = str(ts).encode("ascii") + b"." + body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise SignatureError("signature mismatch")


def _price_catalog() -> dict[str, dict[str, Any]]:
    """price_id -> {tier, modules}. Server-side truth about what a price buys.

    Configured as JSON (env ONCA_BILLING_PRICES), so adding a plan is config, not a
    deploy. Absent/malformed ⇒ empty, which makes every event unrecognised and
    provisioning a no-op — again, fail closed.
    """
    raw = os.environ.get("ONCA_BILLING_PRICES") or ""
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        print("Warning: ONCA_BILLING_PRICES is not valid JSON; no plan will be recognised")
        return {}
    out: dict[str, dict[str, Any]] = {}
    for price_id, spec in (parsed or {}).items():
        if isinstance(spec, dict):
            out[str(price_id)] = {
                "tier": str(spec.get("tier") or "entry"),
                "modules": [str(m).strip().lower() for m in (spec.get("modules") or [])],
            }
    return out


def plan_for_price(price_id: str | None) -> dict[str, Any] | None:
    """What `price_id` entitles. None for an unknown price — an unrecognised plan is
    never silently upgraded to a default."""
    if not price_id:
        return None
    return _price_catalog().get(str(price_id))


def extract_price_id(session: dict[str, Any]) -> str | None:
    """Pull the price id out of a checkout.session.completed payload.

    Stripe only inlines line items when the caller expands them; when it doesn't, the
    price rides on metadata we set at checkout-creation time. Both shapes are read so
    this doesn't depend on how the storefront happens to build the session.
    """
    items = ((session.get("line_items") or {}).get("data")) or []
    for item in items:
        price = (item or {}).get("price") or {}
        if price.get("id"):
            return str(price["id"])
    meta = session.get("metadata") or {}
    return str(meta["price_id"]) if meta.get("price_id") else None


def extract_email(session: dict[str, Any]) -> str | None:
    """The payer's email, from whichever field Stripe populated."""
    for path in (("customer_details", "email"), ("customer_email",)):
        node: Any = session
        for key in path:
            node = (node or {}).get(key) if isinstance(node, dict) else None
        if node:
            return str(node).strip().lower()
    return None


def _table(table: Any | None = None) -> Any:
    if table is not None:
        return table
    import boto3

    name = os.environ.get("ONCA_TENANT_CONFIG_TABLE")
    if not name:
        raise RuntimeError("ONCA_TENANT_CONFIG_TABLE is not configured")
    return boto3.resource("dynamodb").Table(name)


def already_processed(event_id: str, *, table: Any | None = None) -> bool:
    """Has this Stripe event already provisioned? Stripe delivers at-least-once."""
    try:
        got = _table(table).get_item(Key={"tenant_id": IDEMPOTENCY_PK + str(event_id)})
        return bool(got.get("Item"))
    except Exception as exc:  # pragma: no cover - transient
        # Fail OPEN here on purpose: if we cannot READ the marker we would rather risk
        # a duplicate (visible, correctable) than drop a paid provisioning (silent).
        print(f"Warning: idempotency read failed for {event_id}: {exc}")
        return False


def mark_processed(event_id: str, detail: dict[str, Any] | None = None,
                   *, table: Any | None = None) -> None:
    """Record that this event provisioned. Called only AFTER the tenant write."""
    item = {"tenant_id": IDEMPOTENCY_PK + str(event_id),
            "processed_at": int(time.time())}
    item.update({k: v for k, v in (detail or {}).items() if v is not None})
    try:
        _table(table).put_item(Item=item)
    except Exception as exc:  # pragma: no cover - transient
        print(f"Warning: could not record idempotency for {event_id}: {exc}")


def provision_from_session(event: dict[str, Any], *, table: Any | None = None,
                           federated_table: Any | None = None) -> dict[str, Any]:
    """Entitle the payer for a verified checkout.session.completed event.

    Returns a small report; never raises for business-rule misses (unknown price,
    missing email) because those must still return 2xx — Stripe would otherwise retry
    a request that can never succeed, forever.
    """
    from src.dashboard import tenant_config

    event_id = str(event.get("id") or "")
    kind = str(event.get("type") or "")
    if kind != "checkout.session.completed":
        return {"status": "ignored", "reason": f"event type {kind!r}"}
    session = ((event.get("data") or {}).get("object")) or {}
    if str(session.get("payment_status") or "") not in ("paid", "no_payment_required"):
        return {"status": "ignored", "reason": "session not paid"}

    email = extract_email(session)
    plan = plan_for_price(extract_price_id(session))
    if not email:
        return {"status": "error", "reason": "no customer email on session"}
    if not plan:
        return {"status": "error", "reason": "unrecognised price id"}
    if already_processed(event_id, table=table):
        return {"status": "duplicate", "event_id": event_id}

    tenant = tenant_config.self_register_entry_tenant(
        email, plan["modules"], table=table, federated_table=federated_table)
    # Recorded only now: a failure above must stay retryable by Stripe.
    mark_processed(event_id, {"email": email, "tenant": tenant.get("tenant_id")}, table=table)
    return {"status": "provisioned", "event_id": event_id,
            "tenant_id": tenant.get("tenant_id"), "modules": plan["modules"]}


def _resp(code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": code,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(body, ensure_ascii=False)}


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    import base64

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    try:
        verify_signature(raw, headers.get("stripe-signature") or "",
                         os.environ.get("ONCA_STRIPE_WEBHOOK_SECRET"))
    except SignatureError as exc:
        # 400, and deliberately no detail — an attacker probing this endpoint learns
        # nothing about why their forgery failed.
        print(f"Rejected Stripe webhook: {exc}")
        return _resp(400, {"error": "invalid signature"})
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        return _resp(400, {"error": "invalid payload"})
    try:
        report = provision_from_session(payload)
    except Exception as exc:  # pragma: no cover - unexpected
        # 500 so Stripe RETRIES: an infrastructure failure after a real payment must
        # not be swallowed as success.
        print(f"Error provisioning from webhook: {exc}")
        return _resp(500, {"error": "provisioning failed"})
    print(f"Stripe webhook: {report}")
    return _resp(200, report)
