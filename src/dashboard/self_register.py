"""POST /api/register — lazy Entry-tier self-registration for a first-time Google
login (see docs/google-oauth-runbook.md). A Google login that resolves to no
existing `tenant_config.map_federated_email` mapping gets no `custom:tenant` claim
(lambda_pretoken.py's fail-closed-by-omission) and hits the dashboard's honest
"no access" gate; the dashboard offers this endpoint as the one deliberate,
user-initiated escape from that gate — pick entry-tier industries, get an entry
tenant scoped to exactly those, nothing more. Requires an authenticated identity
(the same JWT authorizer as /api/feed) with NO existing tenant — an already-
provisioned caller is rejected rather than silently re-provisioned.
"""
from __future__ import annotations

import json
from typing import Any


def _resp(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    from src.dashboard.auth import identity_from_event

    identity = identity_from_event(event)
    if identity is None:
        return _resp(401, {"error": "unauthorized"})
    if identity.tenant:
        return _resp(409, {"error": "already provisioned", "tenant": identity.tenant})
    from src.dashboard.auth import _claims

    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    if "invite" in body:
        return _redeem(event, identity, body.get("invite"))
    if not identity.email:
        return _resp(400, {"error": "no email on this identity — cannot self-register"})
    # #262: self-registration maps an EMAIL to the new tenant, so only a provider-asserted
    # address may claim one — never a password account, never a self-changed (unverified) email.
    claims = _claims(event) or {}
    if not claims.get("identities") or str(claims.get("email_verified", "")).lower() == "false":
        return _resp(403, {"error": "self-registration needs a Google or Apple sign-in"})

    from src.dashboard.tenant_config import ENTRY_INDUSTRIES, _federated_table, self_register_entry_tenant

    # #249: already linked by an invite (this token predates it) — never a second, separate tenant
    if _federated_table().get_item(Key={"email": "sub#" + identity.sub}).get("Item"):
        return _resp(409, {"error": "already provisioned"})

    industries = body.get("industries")
    try:
        cfg = self_register_entry_tenant(identity.email, industries or [])
    except ValueError as exc:
        return _resp(400, {"error": str(exc), "choices": list(ENTRY_INDUSTRIES)})
    return _resp(200, cfg)


# #249: the same "authenticated, no tenant" caller may instead hold an operator-issued invite code.
_REDEEM_ERRORS = {
    "invalid": (400, "Código inválido ou expirado. Confira com quem enviou o convite."),
    "rate-limited": (429, "Muitas tentativas. Espere um pouco e tente de novo."),
    "already-linked": (409, "Esta conta já está vinculada. Saia e entre de novo."),
}


def _redeem(event: dict[str, Any], identity: Any, code: Any) -> dict[str, Any]:
    """POST /api/register {"invite": code} — link this Google/Apple identity's sub to the code's
    tenant. Federated only (a password user's tenant is set when the operator creates it)."""
    from src.dashboard import invites
    from src.dashboard.auth import _claims

    if not (_claims(event) or {}).get("identities"):
        return _resp(403, {"error": "o código de convite é para entrar com Google ou Apple"})
    r = invites.redeem(code, sub=identity.sub, email=identity.email, ip=invites.client_ip(event))
    if not r["ok"]:
        status, msg = _REDEEM_ERRORS.get(r["reason"], _REDEEM_ERRORS["invalid"])
        return _resp(status, {"error": msg, "reason": r["reason"]})
    return _resp(200, {"linked": True, "tier": r["tier"]})
