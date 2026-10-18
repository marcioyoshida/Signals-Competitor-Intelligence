"""#251 — self-service account deletion ("Excluir minha conta", /exec account sheet and the app).

Reached ONLY through ``/api/me/act`` intent ``delete_account`` with the caller's own verified
Cognito token: there is no target argument, so a caller can only ever delete themselves.

What goes (privacy policy: within 30 days of a request — this runs at once):

* every refresh token (Cognito global sign-out) and then the Cognito user itself;
* the user's decisions, their journal rows, and their engagement events (entities table,
  matched by ``actor`` = sub), plus the per-caller ``ACT#<sub>:`` idempotency replays;
* the user's own rows in the curation journal's ``__act__`` partition (``source`` = sub);
* web/native push subscriptions, the weekly-digest opt-in and its unsubscribe tokens;
* chat reports (``feedback.delete_for_user``) and the chat / report daily quota counters;
* MCP / A2A connected apps (every refresh family revoked, consents deleted);
* the federated e-mail → tenant mapping, so the next Google/Apple sign-in starts fresh.

What stays: the ORGANIZATION's data (tenant settings, its other users, API keys) — it belongs to
the contract. Exception: the sole user of a self-registered Entry tenant (``billing_managed``)
also has that tenant closed (trial ended, no modules). A live paid subscription on such a tenant
blocks the deletion until it is cancelled: we can't stop Stripe billing from here, and deleting
the only person who could cancel would leave a customer paying for nothing.

Order matters: sign-out first (no new token can be minted), data next, the Cognito user LAST — a
failure midway leaves the user able to sign in and retry, and every step is idempotent.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
from typing import Any

CONFIRM_WORD = "EXCLUIR"
_ACT_SUBJECT = "__act__"


class Blocked(Exception):
    """A deletion that must not run (yet). ``status`` is the HTTP code, ``reason`` a short id."""

    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.status, self.reason, self.message = status, reason, message


def user_hash(sub: str) -> str:
    """The pseudonymous handle the deletion's own audit row carries (never the raw sub)."""
    return hashlib.sha256(("onca-deleted:" + str(sub or "")).encode()).hexdigest()[:12]


def _ddb(name_env: str, table: Any | None) -> Any | None:
    if table is not None:
        return table
    name = os.environ.get(name_env)
    if not name:
        return None
    import boto3

    return boto3.resource("dynamodb").Table(name)


def _scan(t: Any, **kw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        r = t.scan(**kw)
        out += r.get("Items") or []
        if not r.get("LastEvaluatedKey"):
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def _query(t: Any, **kw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        r = t.query(**kw)
        out += r.get("Items") or []
        if not r.get("LastEvaluatedKey"):
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


# ---- the tenant: is this the sole user of a self-registered one? ------------------------------
def _other_users(tenant: str, sub: str, email: str, *, cognito: Any, pool: str,
                 federated: Any | None) -> int:
    """How many OTHER people resolve to ``tenant``: federated e-mail mappings plus Cognito users
    whose ``custom:tenant`` attribute names it. Fails closed (an unreadable pool counts as
    'others exist', so a shared tenant is never closed by mistake)."""
    others = 0
    if federated is not None:
        for it in _scan(federated):
            if str(it.get("tenant_id") or "") == tenant and str(it.get("email") or "").lower() != email:
                others += 1
    if cognito is None or not pool:
        return others + 1
    # no AttributesToGet: live, ["sub", "custom:tenant"] makes a full ListUsers fail with
    # InvalidParameterException (a one-user page passes) — the pool is small, take every attribute
    kw: dict[str, Any] = {"UserPoolId": pool}
    while True:
        r = cognito.list_users(**kw)
        for u in r.get("Users") or []:
            attrs = {a.get("Name"): a.get("Value") for a in u.get("Attributes") or []}
            if attrs.get("custom:tenant") == tenant and attrs.get("sub") != sub:
                others += 1
        if not r.get("PaginationToken"):
            return others
        kw["PaginationToken"] = r["PaginationToken"]


def tenant_plan(tenant: str | None, sub: str, email: str, *, tenants: Any | None = None,
                federated: Any | None = None, cognito: Any | None = None,
                pool: str = "") -> dict[str, Any]:
    """What deleting this user does to their tenant: ``{"close": bool, "reason": str}``.
    Raises Blocked when a live paid subscription would be orphaned."""
    if not tenant:
        return {"close": False, "reason": "no-tenant"}
    from src.dashboard import tenant_config

    item = tenant_config._table(tenants).get_item(Key={"tenant_id": tenant}).get("Item")
    if not item or not item.get("billing_managed"):
        return {"close": False, "reason": "organization"}        # operator-provisioned: contract data
    if item.get("closed_at"):
        return {"close": False, "reason": "already-closed"}
    if _other_users(tenant, sub, email, cognito=cognito, pool=pool, federated=federated):
        return {"close": False, "reason": "shared"}
    if any((s or {}).get("status") == "active" for s in (item.get("subs") or {}).values()):
        raise Blocked(409, "active-subscription",
                      "Há uma assinatura ativa nesta conta. Para não continuar sendo cobrado, "
                      "escreva para contato@onssa.org para encerrá-la e depois exclua a conta aqui.")
    return {"close": True, "reason": "sole-user"}


def close_tenant(tenant: str, *, tenants: Any | None = None, now: dt.datetime | None = None) -> bool:
    """End a self-registered tenant: trial over, no trial modules, stamped closed. The row stays
    as the billing tombstone the storefront ``ref`` points at (an unknown ref would page)."""
    from src.dashboard import tenant_config

    now = now or dt.datetime.now(dt.timezone.utc)
    iso = now.isoformat(timespec="seconds")
    try:
        tenant_config._table(tenants).update_item(
            Key={"tenant_id": tenant},
            UpdateExpression="SET closed_at = :n, closed_reason = :r, trial_until = :n, trial_modules = :e",
            ConditionExpression="attribute_exists(tenant_id) AND attribute_not_exists(closed_at)",
            ExpressionAttributeValues={":n": iso, ":r": "account_deleted", ":e": []})
    except Exception as exc:
        if "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc):
            return False
        raise
    return True


# ---- the per-user stores ----------------------------------------------------------------------
def purge_entities(sub: str, *, table: Any | None = None) -> dict[str, Any]:
    """Decisions + engagement the user made, and their act idempotency replays."""
    t = _ddb("ONCA_ENTITIES_TABLE", table)
    if t is None:
        return {"decisions": 0, "engagement": 0, "act_replays": 0, "decision_ids": []}
    from boto3.dynamodb.conditions import Attr

    mine = (Attr("pk").begins_with("DECISION#") | Attr("pk").begins_with("ENGAGEMENT#")) & Attr("actor").eq(sub)
    rows = _scan(t, FilterExpression=mine | Attr("pk").begins_with(f"ACT#{sub}:"),
                 ProjectionExpression="pk, actor")
    # the filter runs server-side; re-check every row before deleting it (never someone else's)
    rows = [it for it in rows if _own_entity_row(it, sub)]
    out = {"decisions": 0, "engagement": 0, "act_replays": 0, "decision_ids": []}
    for it in rows:
        pk = str(it["pk"])
        t.delete_item(Key={"pk": pk})
        if pk.startswith("DECISION#"):
            out["decisions"] += 1
            out["decision_ids"].append(pk[len("DECISION#"):])
        elif pk.startswith("ENGAGEMENT#"):
            out["engagement"] += 1
        else:
            out["act_replays"] += 1
    return out


def _own_entity_row(it: dict[str, Any], sub: str) -> bool:
    pk = str(it.get("pk") or "")
    if pk.startswith(f"ACT#{sub}:"):
        return True
    return pk.startswith(("DECISION#", "ENGAGEMENT#")) and it.get("actor") == sub


def purge_journal(sub: str, decision_ids: list[str], *, table: Any | None = None) -> int:
    """The curation journal's rows about the user's decisions, and their own ``__act__`` rows
    (the act journal records the caller as ``source``; record_decision copies its text there)."""
    t = _ddb("ONCA_CURATION_LOG_TABLE", table)
    if t is None:
        return 0
    from boto3.dynamodb.conditions import Attr, Key

    rows: list[dict[str, Any]] = []
    for did in decision_ids:
        rows += _query(t, KeyConditionExpression=Key("entity_id").eq(f"DECISION#{did}"))
    rows += [it for it in _query(t, KeyConditionExpression=Key("entity_id").eq(_ACT_SUBJECT),
                                 FilterExpression=Attr("source").eq(sub))
             if it.get("entity_id") == _ACT_SUBJECT and it.get("source") == sub]
    for it in rows:
        t.delete_item(Key={"entity_id": it["entity_id"], "ts": it["ts"]})
    return len(rows)


def purge_push(sub: str, *, table: Any | None = None) -> int:
    """Web + native push subscriptions (``SUB#``, field ``user``), the weekly-digest opt-in
    (``DIGEST#<sub>``, which holds the e-mail) and its unsubscribe tokens (``DIGESTTOK#``, ``sub``)."""
    t = _ddb("ONCA_PUSH_TABLE", table)
    if t is None:
        return 0
    from boto3.dynamodb.conditions import Attr

    rows = [it for it in _scan(t, FilterExpression=Attr("user").eq(sub) | Attr("sub").eq(sub)
                               | Attr("pk").eq(f"DIGEST#{sub}"))
            if it.get("user") == sub or it.get("sub") == sub or it.get("pk") == f"DIGEST#{sub}"]
    for it in rows:
        t.delete_item(Key={"pk": it["pk"]})
    return len(rows)


def purge_ask_quota(sub: str, *, table: Any | None = None) -> int:
    t = _ddb("ONCA_ASK_QUOTA_TABLE", table)
    if t is None:
        return 0
    from boto3.dynamodb.conditions import Attr

    rows = [it for it in _scan(t, FilterExpression=Attr("pk").begins_with(f"{sub}#"))
            if str(it.get("pk") or "").startswith(f"{sub}#")]
    for it in rows:
        t.delete_item(Key={"pk": it["pk"]})
    return len(rows)


def purge_feedback(sub: str, *, table: Any | None = None) -> int:
    if table is None and not os.environ.get("ONCA_FEEDBACK_TABLE"):
        return 0
    from src.dashboard import feedback

    return feedback.delete_for_user(sub, table=table)


def revoke_apps(sub: str, *, store: Any | None = None) -> int:
    if store is None:
        name = os.environ.get("ONCA_OAUTH_TABLE")
        if not name:
            return 0
        from src.dashboard.oauth.store import DynamoStore

        store = DynamoStore(name)
    from src.dashboard.oauth import apps

    return apps.revoke_app(store, sub)


def unmap_email(email: str, *, table: Any | None = None) -> int:
    t = table if table is not None else (_ddb("ONCA_FEDERATED_MAP_TABLE", None) if email else None)
    if t is None or not email:
        return 0
    old = t.delete_item(Key={"email": email}, ReturnValues="ALL_OLD").get("Attributes")
    return 1 if old else 0


def _cognito_missing(exc: Exception) -> bool:
    return "UserNotFound" in type(exc).__name__ or "UserNotFound" in str(exc)


# ---- the whole thing --------------------------------------------------------------------------
def delete_account(*, sub: str, username: str | None, email: str | None, tenant: str | None,
                   cognito: Any | None = None, pool: str | None = None,
                   tables: dict[str, Any] | None = None, oauth_store: Any | None = None,
                   now: dt.datetime | None = None) -> dict[str, Any]:
    """Delete one user's account. Returns counts (no identifiers). Raises Blocked."""
    if not sub:
        raise Blocked(400, "no-identity", "sem identidade")
    tables = tables or {}
    pool = pool if pool is not None else os.environ.get("ONCA_USER_POOL_ID", "")
    if cognito is None:
        import boto3

        cognito = boto3.client("cognito-idp")
    email = str(email or "").strip().lower()
    user = str(username or sub)
    fed = tables.get("federated")
    if fed is None:
        fed = _ddb("ONCA_FEDERATED_MAP_TABLE", None)

    plan = tenant_plan(tenant, sub, email, tenants=tables.get("tenants"), federated=fed,
                       cognito=cognito, pool=pool)
    already_gone = False
    try:
        cognito.admin_user_global_sign_out(UserPoolId=pool, Username=user)
    except Exception as exc:
        if not _cognito_missing(exc):
            raise Blocked(502, "signout-failed", "Não foi possível encerrar as sessões; tente de novo.")
        already_gone = True

    ent = purge_entities(sub, table=tables.get("entities"))
    counts: dict[str, Any] = {
        "decisions": ent["decisions"], "engagement": ent["engagement"], "act_replays": ent["act_replays"],
        "journal": purge_journal(sub, ent["decision_ids"], table=tables.get("journal")),
        "push": purge_push(sub, table=tables.get("push")),
        "reports": purge_feedback(sub, table=tables.get("feedback")),
        "ask_quota": purge_ask_quota(sub, table=tables.get("ask_quota")),
        "apps_revoked": revoke_apps(sub, store=oauth_store),
        "federated_mapping": unmap_email(email, table=fed),
        "tenant_closed": bool(plan["close"]) and close_tenant(str(tenant), tenants=tables.get("tenants"), now=now),
        "tenant": plan["reason"],
    }
    if not already_gone:
        try:
            cognito.admin_delete_user(UserPoolId=pool, Username=user)
        except Exception as exc:
            if not _cognito_missing(exc):
                raise Blocked(502, "delete-failed", "Não foi possível concluir a exclusão; tente de novo.")
            already_gone = True
    counts["user_deleted"] = not already_gone
    return counts
