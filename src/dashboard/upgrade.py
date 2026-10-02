"""Upgrade path (#187): the storefront link Onça's CTAs carry, and the Lambda the storefront's
webhook invokes after payment (Signals-Storefront #7 contract, as on Tarantula CMI#105, plus
Onça's per-module extension).

**The link.** ``https://signals-llc.store/?product=onca&tier=<t>&module=<m>&ref=<tenant>&return=…``.
``tier`` is ``entry`` or an ADR 024 SaaS band (``saas_premium``/``saas_mid``/``saas_entry``);
``module`` is the one sector that subscription licenses. ``ref`` is the opaque tenant id (random hex for Entry, #202), never an
email. No link is produced for an invalid tenant id or a module the band doesn't cover: a checkout
the storefront can't attach to an account is how a customer pays for nothing.

**The Lambda.** Input, from the storefront's webhook, async over IAM::

    {"product": "onca", "tenant_ref": "<tenant>", "tier": "entry|saas_*", "module": "<sector>",
     "event": "...", "status": "active|trialing|past_due|canceled|unpaid|...",
     "stripe_event_id": "evt_..."}

One subscription = one module. An active status grants that module; ``subscription.deleted`` or a
terminal status lapses it; ``past_due`` changes nothing (grace, Stripe retries the card). Nothing
is deleted on a lapse, and re-buying restores it.

**Replays.** Each applied ``stripe_event_id`` is recorded (``BILLING#<evt>`` in the tenant-config
table, the marker the retired own-webhook used). A replay is a no-op, so a replayed
``checkout.session.completed`` after a cancellation can't re-grant for free. The record is written
only AFTER the grant/revoke took effect, so a failure leaves it retryable.
"""
from __future__ import annotations

import os
import re
import time
import urllib.parse
from typing import Any

from src.dashboard import tenant_config as tc

PRODUCT = "onca"
TENANT_REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # the storefront's own validator
_ACTIVE = {"active", "trialing"}
_TERMINAL = {"canceled", "unpaid", "incomplete_expired"}
EVENT_PK = "BILLING#"
DEFAULT_RETURN = "https://onssa.org/exec?upgraded=1"


def upgrade_url(tenant: str | None, tier: str, module: str, return_url: str = "") -> str | None:
    """The storefront deep link, or None when no link may be shown."""
    tenant = str(tenant or "")
    if not TENANT_REF_RE.match(tenant) or tenant.startswith(EVENT_PK.rstrip("#")):
        return None
    if tier not in tc.STOREFRONT_TIERS or not tc.module_allowed(tier, module):
        return None
    base = os.environ.get("ONCA_STOREFRONT_URL", "https://signals-llc.store").rstrip("/")
    return "%s/?%s" % (base, urllib.parse.urlencode({
        "product": PRODUCT, "tier": tier, "module": module, "ref": tenant,
        "return": return_url or DEFAULT_RETURN}))


def decide(event: dict[str, Any]) -> str | None:
    """Pure: ``"grant"``, ``"revoke"`` or None for one webhook event."""
    kind = str(event.get("event") or "")
    status = str(event.get("status") or "").lower()
    if kind == "customer.subscription.deleted" or status in _TERMINAL:
        return "revoke"
    if kind in ("checkout.session.completed", "customer.subscription.updated") and status in _ACTIVE:
        return "grant"
    return None   # past_due and friends: keep what they have until a terminal event


class UnknownTenant(Exception):
    """Paid, but no such tenant (or a module the price doesn't cover). Raised, not swallowed, so
    the invocation errors and OncaUpgradeErrorAlarm fires: a paid-but-unprovisioned customer
    must be found in minutes (Storefront #7 §3)."""


def _table():
    import boto3
    return boto3.resource("dynamodb").Table(os.environ["ONCA_TENANT_CONFIG_TABLE"])


def handle(event: dict[str, Any], table: Any) -> dict[str, Any]:
    if str(event.get("product") or "") != PRODUCT:
        print("upgrade: ignored event for another product")
        return {"ok": False, "reason": "product"}
    ref = str(event.get("tenant_ref") or "")
    evt = str(event.get("stripe_event_id") or "")
    if not evt.startswith("evt_"):
        return {"ok": False, "reason": "event_id"}
    if not TENANT_REF_RE.match(ref):
        raise UnknownTenant("invalid tenant_ref")
    if table.get_item(Key={"tenant_id": EVENT_PK + evt}).get("Item"):
        print("upgrade: replay of an already-applied event, no-op")
        return {"ok": True, "replay": True}
    action = decide(event)
    tier, module = str(event.get("tier") or ""), str(event.get("module") or "")
    if action:
        try:
            eff = tc.apply_purchase(ref, tier, module, action, event_id=evt, table=table)
        except (KeyError, ValueError) as exc:
            raise UnknownTenant(type(exc).__name__) from exc
        state = eff["billing"]["state"]
    else:
        state = None
    table.put_item(Item={"tenant_id": EVENT_PK + evt, "tenant": ref, "action": action or "none",
                         "tier": tier, "module": module, "event": str(event.get("event") or ""),
                         "status": str(event.get("status") or ""), "processed_at": int(time.time())})
    # enums only in the log (T16): no tenant id beyond the opaque event id
    print("upgrade: %s applied (%s/%s, %s), billing=%s"
          % (action or "no-op", event.get("event"), event.get("status"), tier, state))
    return {"ok": True, "action": action, "billing": state}


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    return handle(event or {}, _table())
