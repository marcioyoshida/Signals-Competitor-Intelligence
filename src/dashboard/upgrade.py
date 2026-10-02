"""Upgrade path (#187): the storefront link Onça's CTAs carry, and the Lambda the storefront's
webhook invokes after payment (Signals-Storefront #7 contract, as on Tarantula CMI#105, plus
Onça's per-module extension).

**The link.** ``https://signals-llc.store/?product=onca&tier=<t>&module=<m>&ref=<tenant>&return=…``.
``tier`` is ``entry`` or an ADR 024 SaaS band (``saas_premium``/``saas_mid``/``saas_entry``);
``module`` is the one sector that subscription licenses. ``ref`` is the opaque tenant id (random hex for Entry, #202), never an
email. No link is produced for an invalid tenant id or a module the band doesn't cover: a checkout
the storefront can't attach to an account is how a customer pays for nothing.

**Combos** (ADR 024 amendment 2026-10-02): ``…/?product=onca&tier=saas_combo&combo=<id>&ref=<tenant>&return=…``
— no ``module`` param. ``<id>`` is a key of ``tenant_config.COMBOS``, the single source of truth for
which modules a combo licenses; the storefront never sends a module list. No link for an unknown
combo.

**The Lambda.** Input, from the storefront's webhook, async over IAM::

    {"product": "onca", "tenant_ref": "<tenant>", "tier": "entry|saas_*", "module": "<sector>",
     "event": "...", "status": "active|trialing|past_due|canceled|unpaid|...",
     "stripe_event_id": "evt_..."}
    {"product": "onca", "tenant_ref": "<tenant>", "tier": "saas_combo", "combo": "<combo id>",
     "event": "...", "status": "...", "stripe_event_id": "evt_..."}

One subscription = one module, or one combo: for ``"tier": "saas_combo"`` the payload carries
``"combo": "<id>"`` (instead of ``module``) on EVERY event of that subscription, and Onça resolves
the id to its modules. Same semantics either way. An active status grants that module (or every
module of the combo; it is stored as its own ``combo:<id>`` subscription, so revoking it never
removes a module still paid separately); ``subscription.deleted`` or a
terminal status lapses it; ``past_due`` changes nothing (grace, Stripe retries the card). Nothing
is deleted on a lapse, and re-buying restores it.

**Replays.** Each applied ``stripe_event_id`` is recorded (``BILLING#<evt>`` in the tenant-config
table, the marker the retired own-webhook used). A replay is a no-op, so a replayed
``checkout.session.completed`` after a cancellation can't re-grant for free. The marker is CLAIMED
with one conditional write before anything is applied (storefront #11: two copies of one event can
arrive together, and a read-then-write guard let both apply). A failed apply releases the claim, so
the event stays retryable; a claim left ``pending`` by a crashed run is taken over after
``CLAIM_STALE_S``.
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
CLAIM_STALE_S = 300
DEFAULT_RETURN = "https://onssa.org/exec?upgraded=1"


def upgrade_url(tenant: str | None, tier: str, module: str | None = None, return_url: str = "",
                *, combo: str | None = None) -> str | None:
    """The storefront deep link, or None when no link may be shown. Pass ``module`` for a
    per-module subscription, or ``combo`` (with ``tier="saas_combo"``) for a combo — never both."""
    tenant = str(tenant or "")
    if not TENANT_REF_RE.match(tenant) or tenant.startswith(EVENT_PK.rstrip("#")):
        return None
    if combo is not None or tier == tc.COMBO_TIER:
        if module or tier != tc.COMBO_TIER or combo not in tc.COMBOS:
            return None
        what = {"combo": combo}
    elif tier not in tc.STOREFRONT_TIERS or not tc.module_allowed(tier, module):
        return None
    else:
        what = {"module": module}
    base = os.environ.get("ONCA_STOREFRONT_URL", "https://signals-llc.store").rstrip("/")
    return "%s/?%s" % (base, urllib.parse.urlencode({
        "product": PRODUCT, "tier": tier, **what, "ref": tenant,
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
    """Paid, but no such tenant (or a module the price doesn't cover, or an unknown combo). Raised, not swallowed, so
    the invocation errors and OncaUpgradeErrorAlarm fires: a paid-but-unprovisioned customer
    must be found in minutes (Storefront #7 §3)."""


def _table():
    import boto3
    return boto3.resource("dynamodb").Table(os.environ["ONCA_TENANT_CONFIG_TABLE"])


def _claim(table: Any, evt: str, ref: str) -> bool:
    """Atomically claim ``BILLING#<evt>``: True for the first (or a stale-pending) claimant, False
    for a replay or a concurrent duplicate already holding it."""
    from botocore.exceptions import ClientError

    now = int(time.time())
    try:
        table.put_item(
            Item={"tenant_id": EVENT_PK + evt, "tenant": ref, "action": "pending",
                  "claimed_at": now},
            ConditionExpression="attribute_not_exists(tenant_id) OR "
                                "(#a = :pending AND claimed_at < :stale)",
            ExpressionAttributeNames={"#a": "action"},
            ExpressionAttributeValues={":pending": "pending", ":stale": now - CLAIM_STALE_S})
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def _release(table: Any, evt: str) -> None:
    """Drop a claim whose apply failed, so the retried event is applied rather than skipped."""
    try:
        table.delete_item(Key={"tenant_id": EVENT_PK + evt})
    except Exception as exc:  # noqa: BLE001 - a stuck claim still expires after CLAIM_STALE_S
        print(f"upgrade: could not release claim ({type(exc).__name__})")


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
    if not _claim(table, evt, ref):
        print("upgrade: replay of an already-applied event, no-op")
        return {"ok": True, "replay": True}
    action = decide(event)
    tier, module = str(event.get("tier") or ""), str(event.get("module") or "")
    combo = str(event.get("combo") or "")
    if action:
        try:
            if tier == tc.COMBO_TIER:
                eff = tc.apply_combo_purchase(ref, combo, action, event_id=evt, table=table)
            else:
                eff = tc.apply_purchase(ref, tier, module, action, event_id=evt, table=table)
        except (KeyError, ValueError) as exc:
            _release(table, evt)
            raise UnknownTenant(type(exc).__name__) from exc
        except Exception:
            _release(table, evt)
            raise
        state = eff["billing"]["state"]
    else:
        state = None
    table.put_item(Item={"tenant_id": EVENT_PK + evt, "tenant": ref, "action": action or "none",
                         "tier": tier, "module": module, "combo": combo, "event": str(event.get("event") or ""),
                         "status": str(event.get("status") or ""), "processed_at": int(time.time())})
    # enums only in the log (T16): no tenant id beyond the opaque event id
    print("upgrade: %s applied (%s/%s, %s), billing=%s"
          % (action or "no-op", event.get("event"), event.get("status"), tier, state))
    return {"ok": True, "action": action, "billing": state}


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    return handle(event or {}, _table())
