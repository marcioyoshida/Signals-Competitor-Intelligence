"""#249 — invite codes: link a federated login (Google / Apple) to its organization's tenant.

The e-mail map (``tenant_config.map_federated_email``) only works when the address Onça receives is
the corporate one the operator registered. Apple's Hide My Email hands us
``…@privaterelay.appleid.com`` instead, which never matches — and an unmatched federated login is
offered Entry self-registration, so an invited officer could quietly create a tiny separate
tenant. An invite code closes that gap: the operator issues one per person (``issue_invite`` act,
journaled), the person redeems it from the no-access gate, and the link is keyed by the
identity's Cognito ``sub`` — the relay address is kept only as a contact field.

Rows live in the existing federated-map table (pk ``email``, TTL ``ttl``), under prefixes no real
address can take:

  ``invite#<sha256>``   one code: tenant, tier, expiry, used_at/used_by (only the hash is stored)
  ``sub#<sub>``         the link the pre-token trigger reads first: tenant, tier, contact e-mail
  ``rl#sub|ip#…#<hour>`` redemption attempt counters (TTL 2 h)

Codes are 20 Crockford base-32 characters (100 bits) shown as 4 groups of 5. A failure never says
whether a code exists: unknown, expired and already-used all read "invalid".
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
import time
from typing import Any

INVITE, SUB, RL = "invite#", "sub#", "rl#"
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford: no I, L, O, U
CODE_LEN = 20
DEFAULT_DAYS, MAX_DAYS = 7, 30
LIMIT_PER_SUB, LIMIT_PER_IP = 5, 20              # attempts per hour


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def normalize(code: Any) -> str:
    """What the person typed → the canonical code (case, dashes, spaces and look-alikes forgiven)."""
    s = re.sub(r"[\s\-_.]", "", str(code or "")).upper()
    return s.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))


def code_hash(code: Any) -> str:
    return hashlib.sha256(("onca-invite:" + normalize(code)).encode()).hexdigest()


def _display(raw: str) -> str:
    return "-".join(raw[i:i + 5] for i in range(0, len(raw), 5))


def issue(tenant_id: str, tier: str | None = None, *, issued_by: str, days: Any = DEFAULT_DAYS,
          label: str | None = None, table: Any | None = None, tenants: Any | None = None,
          now: dt.datetime | None = None) -> dict[str, Any]:
    """A new single-use code for ``tenant_id``. Returns the code ONCE (only its hash is stored).
    Raises ValueError on an unknown tenant, tier or validity."""
    from src.dashboard import tenant_config

    tenant_id = str(tenant_id or "").strip()
    cfg = tenant_config.get_tenant_config(tenant_id, table=tenants) if tenant_id else None
    if not cfg:
        raise ValueError("unknown tenant")
    tier = str(tier or cfg.get("tier") or "").strip().lower()
    if tier not in tenant_config.VALID_TIERS:
        raise ValueError(f"tier must be one of {list(tenant_config.VALID_TIERS)}")
    try:
        days = int(days if days not in (None, "") else DEFAULT_DAYS)
    except (TypeError, ValueError):
        raise ValueError("days must be a number") from None
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be 1–{MAX_DAYS}")
    now = now or _now()
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    expires = now + dt.timedelta(days=days)
    item = {"email": INVITE + code_hash(raw), "type": "invite", "tenant_id": tenant_id, "tier": tier,
            "issued_by": str(issued_by), "created_at": _iso(now), "expires_at": _iso(expires),
            "ttl": int(expires.timestamp()) + 30 * 86400}
    if label:
        item["label"] = str(label).strip()[:60]
    tenant_config._federated_table(table).put_item(Item=item)
    return {"code": _display(raw), "tenant_id": tenant_id, "tier": tier, "expires_at": item["expires_at"]}


def _take(t: Any, key: str, limit: int, now: dt.datetime) -> bool:
    """One attempt against an hourly counter; False once ``limit`` is reached."""
    from boto3.dynamodb.conditions import Attr

    try:
        t.update_item(Key={"email": key}, UpdateExpression="ADD n :one SET #t = :t",
                      ConditionExpression=Attr("n").not_exists() | Attr("n").lt(limit),
                      ExpressionAttributeNames={"#t": "ttl"},   # TTL is a reserved word
                      ExpressionAttributeValues={":one": 1, ":t": int(now.timestamp()) + 7200})
        return True
    except Exception as exc:
        if "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc):
            return False
        raise


def redeem(code: Any, *, sub: str, email: str | None, ip: str | None, table: Any | None = None,
           now: dt.datetime | None = None) -> dict[str, Any]:
    """Link ``sub`` to the code's tenant. ``{"ok": True, tenant_id, tier}`` or
    ``{"ok": False, "reason": "invalid" | "rate-limited" | "already-linked"}``."""
    from boto3.dynamodb.conditions import Attr

    from src.dashboard import tenant_config

    t = tenant_config._federated_table(table)
    now = now or _now()
    hour = now.strftime("%Y%m%d%H")
    if not sub:
        return {"ok": False, "reason": "invalid"}
    if not _take(t, f"{RL}sub#{sub}#{hour}", LIMIT_PER_SUB, now) or (
            ip and not _take(t, f"{RL}ip#{ip}#{hour}", LIMIT_PER_IP, now)):
        return {"ok": False, "reason": "rate-limited"}
    if t.get_item(Key={"email": SUB + sub}).get("Item"):
        return {"ok": False, "reason": "already-linked"}
    raw = normalize(code)
    if len(raw) != CODE_LEN or any(c not in ALPHABET for c in raw):
        return {"ok": False, "reason": "invalid"}
    key = {"email": INVITE + code_hash(raw)}
    try:   # single use, unexpired — atomically
        got = t.update_item(
            Key=key, UpdateExpression="SET used_at = :n, used_by = :s",
            ConditionExpression=Attr("tenant_id").exists() & Attr("used_at").not_exists()
            & Attr("expires_at").gt(_iso(now)),
            ExpressionAttributeValues={":n": _iso(now), ":s": sub}, ReturnValues="ALL_NEW")
    except Exception as exc:
        if "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc):
            return {"ok": False, "reason": "invalid"}
        raise
    inv = got.get("Attributes") or {}
    link = {"email": SUB + sub, "type": "sub_link", "tenant_id": str(inv["tenant_id"]),
            "tier": str(inv.get("tier") or "saas"), "via": "invite", "created_at": _iso(now)}
    if email:
        link["contact_email"] = str(email).strip().lower()   # a contact field, never the key
    t.put_item(Item=link)
    return {"ok": True, "tenant_id": link["tenant_id"], "tier": link["tier"]}


def client_ip(event: dict[str, Any]) -> str | None:
    """The viewer IP CloudFront appended to X-Forwarded-For: the rightmost entry that isn't the
    immediate peer. Entries a client sends itself can only appear further LEFT."""
    rc = (event or {}).get("requestContext") or {}
    peer = str(((rc.get("http") or {}).get("sourceIp")) or "")
    hdrs = {str(k).lower(): v for k, v in ((event or {}).get("headers") or {}).items()}
    parts = [p.strip() for p in str(hdrs.get("x-forwarded-for") or "").split(",") if p.strip()]
    while parts and parts[-1] == peer:
        parts.pop()
    return parts[-1] if parts else (peer or None)


def linked_tenant(sub: str, *, table: Any | None = None) -> str | None:
    from src.dashboard import tenant_config

    it = tenant_config._federated_table(table).get_item(Key={"email": SUB + str(sub)}).get("Item") if sub else None
    return str(it["tenant_id"]) if it else None


def is_person_row(key: Any) -> bool:
    """A federated-map row that stands for a PERSON (an e-mail mapping or a sub link) — not an
    invite or a rate counter. Used to count a tenant's users."""
    k = str(key or "")
    return k.startswith(SUB) or (not k.startswith((INVITE, RL)) and "@" in k)
