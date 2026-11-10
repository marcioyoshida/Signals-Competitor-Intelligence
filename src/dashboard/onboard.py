"""#379 — operator onboarding (the ``onca-onboard`` Lambda) for the fleet onboarding panel
(Signals-Fleet-Monitor #18). Synchronous ``RequestResponse`` invoke over IAM; no URL, no route.

Request (the contract shared with Onça Setorial's ``anteater-onboard``)::

    {"action": "put | link | get | expire | remove", "request_id": "...", "cohort":
     "friends-family | pilot | design-partner", "tenant_id": "slug", "tier": "...", "modules": [...],
     "email": "...", "login": "password | google", "send_invite": false,
     "expires_on": "YYYY-MM-DD" | null, "actor": "fleet-monitor", "mode": "disable | delete"}

Response: ``{"ok": true, "request_id", "tenant_id", "tier", "modules", "user": "created | updated |
mapped | none", "onboard": {"cohort", "expires_on"} | null, ...}`` or ``{"ok": false, "request_id",
"error": "REJECTED: ..."}``. A rejection is RETURNED (the panel shows the fork's own message); only
an unexpected failure raises.

What each action may touch:

``put``     creates or re-puts a tenant THIS module created, through ``tenant_config.put_tenant_config``
            (every existing guard applies). The row carries ``onboard = {cohort, expires_on, by, at,
            request_id}``, written in the same put. Refused for a billing-managed tenant, one with
            any ``subs`` entry, and an existing row without the stamp (a hand-provisioned or
            self-registered tenant is never adopted: adopting it would make it removable).
``link``    attaches one login to an EXISTING tenant (any tenant, several e-mails) and changes
            nothing on its row. A ``friends-family`` guest is refused on a billing-managed
            tenant or one with ``subs``: a guest never lands on a customer's tenant.
``get``     the tenant's entitlement, its stamp and how many logins onboarding tracks.
``expire``  lapses the comp: base ``modules`` become ``[]`` (a purchase in ``subs`` still entitles its
            own modules) and, when the tenant has no purchase, the logins onboarding created stop
            working (``mode`` ``disable``, the default, or ``delete``).
``remove``  deletes the logins onboarding created and the row. Refused when the row has a purchase.

``expire`` / ``remove`` need the stamp, except with ``email``: then they act on that one login,
and only if onboarding created it (the friends-and-family case: logins linked to a shared demo
tenant that onboarding does not own).

**What onboarding created** is tracked in the federated-map table, which already holds the
people (the tenant-config table holds no personal data, and ``link`` must not change the row):

* a Google mapping row (key = the e-mail) carries its own ``onboard`` attribute; a mapping that
  existed before is left unmarked, so it is never removed here;
* a password login gets ``onboard#<tenant_id>#<cognito username>`` (the opaque Cognito id, not the
  e-mail), written only when THIS module created the Cognito user. Not a person row for
  ``invites.is_person_row``, so it never counts as a user of the tenant.

``provision`` / ``attach_login`` are the writes shared with ``scripts/provision_tenant.py put``,
so the CLI and the Lambda cannot drift. The CLI does not stamp: a hand-provisioned tenant stays
out of ``expire`` / ``remove``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from typing import Any, Callable

from src.dashboard import tenant_config as tc
from src.dashboard.upgrade import EVENT_PK, TENANT_REF_RE

FUNCTION = "onca-onboard"
ACTIONS = ("put", "link", "get", "expire", "remove")
COHORTS = ("friends-family", "pilot", "design-partner")
LOGINS = ("password", "google")
EXPIRE_MODES = ("disable", "delete")
MARK = "onboard#"
# federated-map key prefixes no e-mail may take (src/dashboard/invites.py owns the first three)
_RESERVED = ("invite#", "sub#", "rl#", MARK)
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MUTATING = ("put", "link", "expire", "remove")

Step = Callable[[str, Any], None]


class Rejected(Exception):
    """A request this fork refuses. ``stage`` says which rule (never any personal data): the
    Lambda logs it, the CLI picks its message prefix from it."""

    def __init__(self, message: str, *, stage: str = "request") -> None:
        super().__init__(message)
        self.stage = stage


# ---- the writes shared with scripts/provision_tenant.py ---------------------------------------
def attach_login(tenant_id: str, tier: str, *, email: str | None = None,
                 google_email: str | None = None, pool_id: str | None = None,
                 send_invite: bool = True, federated_table: Any | None = None,
                 cognito: Any | None = None, on_step: Step | None = None) -> dict[str, str]:
    """Link logins to a tenant: ``email`` = a Cognito password user (`custom:tenant`/`custom:tier`),
    ``google_email`` = the e-mail -> tenant mapping a Google login resolves through. Returns
    ``{"cognito": "created" | "updated", "google": "mapped"}`` for the ones asked for; raises
    Rejected (stage ``pool`` / ``cognito`` / ``google``) with the underlying guard's message."""
    out: dict[str, str] = {}
    if email:
        pool = pool_id or os.environ.get("ONCA_USER_POOL_ID")
        if not pool:
            raise Rejected("no Cognito user pool id ($ONCA_USER_POOL_ID)", stage="pool")
        try:
            out["cognito"] = tc.cognito_upsert_user(pool, email, tenant_id, tier, client=cognito,
                                                    send_invite=send_invite)
        except ValueError as exc:
            raise Rejected(str(exc), stage="cognito") from exc
        if on_step:
            on_step("cognito", out["cognito"])
    if google_email:
        try:
            tc.map_federated_email(google_email, tenant_id, tier, table=federated_table)
        except ValueError as exc:
            raise Rejected(str(exc), stage="google") from exc
        out["google"] = "mapped"
        if on_step:
            on_step("google", "mapped")
    return out


def provision(tenant_id: str, tier: str, modules: list[str], *, plane: str | None = None,
              force_not_ready: bool = False, resolve_caller_role_arn: str | None = None,
              onboard: dict[str, Any] | None = None, table: Any | None = None,
              email: str | None = None, google_email: str | None = None,
              pool_id: str | None = None, send_invite: bool = True,
              federated_table: Any | None = None, cognito: Any | None = None,
              on_step: Step | None = None) -> dict[str, Any]:
    """Write the tenant row through ``put_tenant_config`` (its guards unchanged), then the logins.
    ``on_step(stage, value)`` fires after each write that happened (``tenant`` with the stored
    config, then ``cognito`` / ``google``), so a caller can report progress as it goes. Raises
    Rejected with stage ``tenant`` when a guard refuses the row (nothing is written)."""
    try:
        cfg = tc.put_tenant_config(tenant_id, tier, modules, plane=plane, table=table,
                                   force_not_ready=force_not_ready,
                                   resolve_caller_role_arn=resolve_caller_role_arn, onboard=onboard)
    except ValueError as exc:
        raise Rejected(str(exc), stage="tenant") from exc
    if on_step:
        on_step("tenant", cfg)
    logins = attach_login(cfg["tenant_id"], cfg["tier"], email=email, google_email=google_email,
                          pool_id=pool_id, send_invite=send_invite,
                          federated_table=federated_table, cognito=cognito, on_step=on_step)
    return {"config": cfg, "logins": logins}


# ---- small helpers ----------------------------------------------------------------------------
def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def email_hash(email: str) -> str:
    """The only form an e-mail takes in a log line."""
    return hashlib.sha256(("onca-onboard:" + str(email).strip().lower()).encode()).hexdigest()[:12]


def _conditional(exc: Exception) -> bool:
    return "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc)


def _missing_user(exc: Exception) -> bool:
    return "UserNotFound" in type(exc).__name__ or "UserNotFound" in str(exc)


def _scan(t: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    kw: dict[str, Any] = {}
    while True:
        page = t.scan(**kw)
        out.extend(page.get("Items") or [])
        if not page.get("LastEvaluatedKey"):
            return out
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _summary(stamp: Any) -> dict[str, Any] | None:
    """The stamp as the response carries it."""
    if not stamp:
        return None
    out = {"cohort": stamp.get("cohort"), "expires_on": stamp.get("expires_on")}
    if stamp.get("expired_at"):
        out["expired_at"] = stamp["expired_at"]
    return out


class _Ctx:
    """The stores, resolved lazily so a call only builds the clients it uses."""

    def __init__(self, table: Any, federated_table: Any, cognito: Any, pool_id: str | None) -> None:
        self._table, self._fed, self._cognito, self._pool = table, federated_table, cognito, pool_id

    @property
    def tenants(self) -> Any:
        self._table = tc._table(self._table)
        return self._table

    @property
    def fed(self) -> Any:
        self._fed = tc._federated_table(self._fed)
        return self._fed

    @property
    def cognito(self) -> Any:
        if self._cognito is None:
            import boto3

            self._cognito = boto3.client("cognito-idp")
        return self._cognito

    @property
    def pool(self) -> str:
        pool = self._pool or os.environ.get("ONCA_USER_POOL_ID")
        if not pool:   # a deploy fault, not something the operator typed: raise, don't reject
            raise RuntimeError("ONCA_USER_POOL_ID is not set")
        return pool

    def raw(self, tenant_id: str) -> dict[str, Any] | None:
        """The RAW row (``get_tenant_config`` is a derived view, and hides a lookup error)."""
        return self.tenants.get_item(Key={"tenant_id": tenant_id}).get("Item")

    def view(self, tenant_id: str) -> dict[str, Any]:
        cfg = tc.get_tenant_config(tenant_id, table=self.tenants)
        if cfg is None:
            raise RuntimeError("tenant lookup failed")
        return cfg


# ---- request validation -----------------------------------------------------------------------
def _parse(event: Any, log: dict[str, Any]) -> dict[str, Any]:
    """Validate the request; fills ``log`` only with values that passed (so a mistyped field can
    never put an e-mail in the log line)."""
    if not isinstance(event, dict):
        raise Rejected("the event must be a JSON object")
    rid = event.get("request_id")
    if not isinstance(rid, str) or not _ID_RE.fullmatch(rid):
        raise Rejected("request_id is required (letters, digits and . _ : - ; at most 64)")
    log["request_id"] = rid
    action = event.get("action")
    if action not in ACTIONS:
        raise Rejected(f"action must be one of {list(ACTIONS)}")
    log["action"] = action
    tenant = event.get("tenant_id")
    if (not isinstance(tenant, str) or not TENANT_REF_RE.match(tenant)
            or tenant.startswith(EVENT_PK.rstrip("#"))):
        raise Rejected("tenant_id must be a slug (letters, digits, _ and -; at most 64)")
    log["tenant"] = tenant
    cohort = event.get("cohort")
    if cohort is not None or action in ("put", "link"):
        if cohort not in COHORTS:
            raise Rejected(f"cohort must be one of {list(COHORTS)}")
        log["cohort"] = cohort
    actor = event.get("actor")
    if actor is not None or action in _MUTATING:
        if not isinstance(actor, str) or not _ID_RE.fullmatch(actor):
            raise Rejected("actor is required (who asked: letters, digits and . _ : - ; at most 64)")
        log["actor"] = actor
    email = event.get("email")
    if email is not None or action == "link":
        email = email.strip().lower() if isinstance(email, str) else ""
        if not _EMAIL_RE.fullmatch(email) or email.startswith(_RESERVED):
            raise Rejected("email must be one e-mail address")
        log["email_sha"] = email_hash(email)
    login = event.get("login")
    if login is not None and login not in LOGINS:
        raise Rejected(f"login must be one of {list(LOGINS)}")
    if email and login is None and action in ("put", "link"):
        raise Rejected(f"login is required with email: one of {list(LOGINS)}")
    send_invite = event.get("send_invite", False)
    if not isinstance(send_invite, bool):
        raise Rejected("send_invite must be true or false")
    if send_invite and (login != "password" or not email or action not in ("put", "link")):
        raise Rejected("send_invite only applies to put/link of a password login")
    expires_on = event.get("expires_on")
    if expires_on is not None:
        try:
            if not isinstance(expires_on, str) or not _DATE_RE.fullmatch(expires_on):
                raise ValueError
            dt.date.fromisoformat(expires_on)
        except ValueError:
            raise Rejected("expires_on must be YYYY-MM-DD or null") from None
    mode = event.get("mode", "disable")
    if action == "expire" and mode not in EXPIRE_MODES:
        raise Rejected(f"mode must be one of {list(EXPIRE_MODES)}")
    req = {"action": action, "request_id": rid, "tenant_id": tenant, "cohort": cohort,
           "actor": actor, "email": email or None, "login": login, "send_invite": send_invite,
           "expires_on": expires_on, "mode": mode}
    if action == "put":
        tier, modules = event.get("tier"), event.get("modules")
        if not isinstance(tier, str) or not tier:
            raise Rejected(f"tier is required for put: one of {list(tc.VALID_TIERS)}")
        if (not isinstance(modules, list) or not modules
                or not all(isinstance(m, str) and m.strip() for m in modules)):
            raise Rejected("modules is required for put: a non-empty list of module slugs")
        req["tier"] = tier
        req["modules"] = sorted({m.strip().lower() for m in modules})
    return req


def _stamp(req: dict[str, Any]) -> dict[str, Any]:
    """{cohort, expires_on, by, at, request_id}. ``expires_on`` is omitted when null (this table
    never stores a NULL attribute); readers use ``.get``."""
    out = {"cohort": req["cohort"], "by": req["actor"], "at": _now(), "request_id": req["request_id"]}
    if req["expires_on"]:
        out["expires_on"] = req["expires_on"]
    return out


# ---- logins: what existed before, and what onboarding created ---------------------------------
def _marker_key(tenant_id: str, username: str) -> str:
    """``username`` is Cognito's own id for the user. On this pool (e-mail is the username
    ATTRIBUTE) that is the opaque ``sub``, never the address — checked, because a key with an
    e-mail in it would be personal data and would count as a person row."""
    if "@" in username:
        raise RuntimeError("the Cognito username is not opaque (an e-mail, expected the sub)")
    return f"{MARK}{tenant_id}#{username}"


def _preflight(req: dict[str, Any], ctx: _Ctx) -> dict[str, Any] | None:
    """Read-only: refuse a login this call could not attach, BEFORE anything is written, and
    remember what already existed (so only what onboarding creates is tracked as its own)."""
    email, tenant = req["email"], req["tenant_id"]
    if not email:
        return None
    if req["login"] == "google":
        row = ctx.fed.get_item(Key={"email": email}).get("Item")
        if row and str(row.get("tenant_id") or "") != tenant:
            raise Rejected("this e-mail's Google login is already mapped to another tenant",
                           stage="google")
        return {"ours": row is None or bool(row.get("onboard"))}
    try:
        user = tc.cognito_user_link(ctx.pool, email, tenant, client=ctx.cognito)
    except ValueError as exc:   # the existing guard: linked to another tenant
        raise Rejected(str(exc), stage="cognito") from exc
    if user is None:
        return {"user": None, "marker": None}
    if not user["tenant"]:
        # cognito_upsert_user would report "updated", but custom:tenant can never be set on an
        # existing user: the login would resolve to no tenant at all.
        raise Rejected("this e-mail already has a login with no tenant, and custom:tenant is "
                       "immutable: use the Google login or another e-mail", stage="cognito")
    marker = ctx.fed.get_item(Key={"email": _marker_key(tenant, user["username"])}).get("Item")
    return {"user": user, "marker": marker}


def _track(req: dict[str, Any], ctx: _Ctx, pre: dict[str, Any] | None,
           logins: dict[str, str], stamp: dict[str, Any]) -> dict[str, Any]:
    """After the login was attached: mark what onboarding created (with this request's
    ``stamp``), wake what it had disabled. Returns the response's ``user`` and ``login`` fields."""
    email, tenant = req["email"], req["tenant_id"]
    if not email or pre is None:
        return {"user": "none", "login": None}
    info: dict[str, Any] = {"type": req["login"], "cohort": req["cohort"],
                            "expires_on": req["expires_on"]}
    if req["login"] == "google":
        info["tracked"] = bool(pre["ours"])
        if pre["ours"]:   # map_federated_email re-puts the row, so the mark is set again each time
            ctx.fed.update_item(
                Key={"email": email}, UpdateExpression="SET #o = :o",
                ConditionExpression="attribute_exists(#e)",
                ExpressionAttributeNames={"#o": "onboard", "#e": "email"},
                ExpressionAttributeValues={":o": stamp})
        return {"user": logins.get("google", "mapped"), "login": info}
    outcome, marker, user = logins["cognito"], pre["marker"], pre["user"]
    info["invite"] = "none"
    if outcome == "created":
        user = tc.cognito_user_link(ctx.pool, email, tenant, client=ctx.cognito)
        if user is None:
            raise RuntimeError("the Cognito user just created cannot be read back")
        info["invite"] = "sent" if req["send_invite"] else "suppressed"
        marker = None
    elif marker is None:
        info["tracked"] = False   # a login that existed before onboarding: never reversed here
        return {"user": outcome, "login": info}
    else:
        if marker.get("disabled_at"):
            ctx.cognito.admin_enable_user(UserPoolId=ctx.pool, Username=user["username"])
        if req["send_invite"] and user["status"] == "FORCE_CHANGE_PASSWORD":
            # never signed in: Cognito re-sends its invitation with a fresh temporary password
            ctx.cognito.admin_create_user(UserPoolId=ctx.pool, Username=email,
                                          MessageAction="RESEND", DesiredDeliveryMediums=["EMAIL"])
            info["invite"] = "resent"
    info["tracked"] = True
    prior = (marker or {}).get("onboard") or {}
    if (marker is None or marker.get("disabled_at") or prior.get("cohort") != req["cohort"]
            or prior.get("expires_on") != req["expires_on"]):
        ctx.fed.put_item(Item={"email": _marker_key(tenant, user["username"]),
                               "type": "onboard_login", "tenant_id": tenant, "login": "password",
                               "user": user["username"], "onboard": stamp})
    return {"user": outcome, "login": info}


def _tracked(ctx: _Ctx, tenant_id: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Every login onboarding created for the tenant: ``(kind, federated-map key, row)``."""
    prefix, out = _marker_key(tenant_id, ""), []
    for it in _scan(ctx.fed):
        key = str(it.get("email") or "")
        if key.startswith(prefix):
            out.append(("password", key, it))
        elif (it.get("onboard") and "@" in key and not key.startswith(_RESERVED)
              and str(it.get("tenant_id") or "") == tenant_id):
            out.append(("google", key, it))
    return out


def _tracked_for(req: dict[str, Any], ctx: _Ctx) -> list[tuple[str, str, dict[str, Any]]]:
    """The logins onboarding created for ONE e-mail on the tenant (``login`` narrows the kind)."""
    email, tenant, out = req["email"], req["tenant_id"], []
    if req["login"] in (None, "google"):
        row = ctx.fed.get_item(Key={"email": email}).get("Item")
        if row and row.get("onboard") and str(row.get("tenant_id") or "") == tenant:
            out.append(("google", email, row))
    if req["login"] in (None, "password"):
        try:
            user = tc.cognito_user_link(ctx.pool, email, tenant, client=ctx.cognito)
        except ValueError:   # belongs to another tenant: not this tenant's login
            user = None
        if user:
            key = _marker_key(tenant, user["username"])
            marker = ctx.fed.get_item(Key={"email": key}).get("Item")
            if marker:
                out.append(("password", key, marker))
    return out


def _reverse(ctx: _Ctx, tenant_id: str, kind: str, key: str, row: dict[str, Any],
             mode: str) -> str:
    """Undo one login onboarding created. ``disabled`` | ``deleted`` | ``gone`` (already absent)."""
    if kind == "google":
        # a mapping has no "disabled" state (the pre-token trigger reads the row as it is), so
        # both modes delete it — and only while it is still onboarding's and still this tenant's
        try:
            ctx.fed.delete_item(
                Key={"email": key}, ConditionExpression="attribute_exists(#o) AND #t = :t",
                ExpressionAttributeNames={"#o": "onboard", "#t": "tenant_id"},
                ExpressionAttributeValues={":t": tenant_id})
        except Exception as exc:
            if not _conditional(exc):
                raise
            return "gone"
        return "deleted"
    username = str(row.get("user") or key[len(_marker_key(tenant_id, "")):])
    try:
        if mode == "delete":
            ctx.cognito.admin_delete_user(UserPoolId=ctx.pool, Username=username)
        else:
            ctx.cognito.admin_disable_user(UserPoolId=ctx.pool, Username=username)
    except Exception as exc:
        if not _missing_user(exc):
            raise
        ctx.fed.delete_item(Key={"email": key})
        return "gone"
    if mode == "delete":
        ctx.fed.delete_item(Key={"email": key})
        return "deleted"
    if not row.get("disabled_at"):
        ctx.fed.update_item(Key={"email": key}, UpdateExpression="SET #d = :n",
                            ExpressionAttributeNames={"#d": "disabled_at"},
                            ExpressionAttributeValues={":n": _now()})
    return "disabled"


def _reverse_all(ctx: _Ctx, tenant_id: str, logins: list[tuple[str, str, dict[str, Any]]],
                 mode: str) -> dict[str, int]:
    counts = {"disabled": 0, "deleted": 0, "gone": 0}
    for kind, key, row in logins:
        counts[_reverse(ctx, tenant_id, kind, key, row, mode)] += 1
    return counts


def _existing(req: dict[str, Any], ctx: _Ctx) -> dict[str, Any]:
    raw = ctx.raw(req["tenant_id"])
    if raw is None:
        raise Rejected(f"unknown tenant {req['tenant_id']!r}", stage="unknown-tenant")
    return raw


def _one_login(req: dict[str, Any], ctx: _Ctx, raw: dict[str, Any], mode: str) -> dict[str, Any]:
    """expire/remove with ``email``: that login only, and only if onboarding created it."""
    logins = _tracked_for(req, ctx)
    if not logins:
        raise Rejected("this login was not created by onboarding; expire and remove only reverse "
                       "what onboarding created", stage="not-onboarded")
    cfg = ctx.view(req["tenant_id"])
    return {"tenant_id": req["tenant_id"], "tier": cfg["tier"], "modules": cfg["modules"],
            "user": "none", "onboard": _summary(raw.get("onboard")), "scope": "login",
            "logins": {**_reverse_all(ctx, req["tenant_id"], logins, mode), "kept": False}}


def _needs_stamp(req: dict[str, Any], raw: dict[str, Any]) -> None:
    if not tc.is_onboarded(raw):
        raise Rejected(f"tenant {req['tenant_id']!r} was not created by onboarding (no onboard "
                       f"stamp); {req['action']} only reverses what onboarding created — pass "
                       "email to act on one login it linked", stage="not-onboarded")


# ---- the actions ------------------------------------------------------------------------------
def _put(req: dict[str, Any], ctx: _Ctx) -> dict[str, Any]:
    tenant, tier, mods = req["tenant_id"], req["tier"], req["modules"]
    raw = ctx.raw(tenant)
    if raw is not None:
        if raw.get("billing_managed") or raw.get("subs"):
            raise Rejected(f"tenant {tenant!r} is billing-managed (a purchase or a trial) and is "
                           "never rewritten from onboarding; use link to add a login to it",
                           stage="billing")
        if not tc.is_onboarded(raw):
            raise Rejected(f"tenant {tenant!r} already exists and was not created by onboarding; "
                           "it is never rewritten here — use link to add a login to it",
                           stage="not-onboarded")
    if tier in tc.VALID_TIERS and tc.allowed_industries_for_tier(tier) is None:
        # put_tenant_config allow-lists Entry only; a typo on another tier would be stored as a
        # module nothing ever feeds
        from src.synth.entity_registry import INDUSTRIES

        unknown = [m for m in mods if m not in INDUSTRIES]
        if unknown:
            raise Rejected(f"unknown module(s) {unknown}; known: {sorted(INDUSTRIES)}",
                           stage="tenant")
    pre = _preflight(req, ctx)
    prior = (raw or {}).get("onboard") or {}
    same = bool(raw) and (raw.get("tier") == tier and list(raw.get("modules") or []) == mods
                          and prior.get("cohort") == req["cohort"]
                          and prior.get("expires_on") == req["expires_on"]
                          and not prior.get("expired_at"))
    # an unchanged re-put keeps the stamp it has, so the row is rewritten identical (a no-op);
    # the guards still run. plane / resolve_caller_role_arn are not in the contract: kept as set.
    fresh = _stamp(req)
    stamp = dict(prior) if same else fresh
    google = req["login"] == "google"
    try:
        done = provision(tenant, tier, mods, plane=(raw or {}).get("plane"),
                         resolve_caller_role_arn=(raw or {}).get("resolve_caller_role_arn"),
                         onboard=stamp, table=ctx.tenants,
                         email=None if google else req["email"],
                         google_email=req["email"] if google else None,
                         pool_id=ctx.pool if req["email"] and not google else None,
                         send_invite=req["send_invite"], federated_table=ctx.fed,
                         cognito=ctx.cognito if req["email"] and not google else None)
    except Rejected as exc:
        if exc.stage == "tenant":
            raise
        raise Rejected(f"{exc} (the tenant row WAS written)", stage=exc.stage) from exc
    cfg = done["config"]
    return {"tenant_id": cfg["tenant_id"], "tier": cfg["tier"], "modules": cfg["modules"],
            **_track(req, ctx, pre, done["logins"], fresh), "onboard": _summary(stamp),
            "changed": not same}


def _link(req: dict[str, Any], ctx: _Ctx) -> dict[str, Any]:
    raw = _existing(req, ctx)
    if req["cohort"] == "friends-family" and (raw.get("billing_managed") or raw.get("subs")):
        # link reaches ANY tenant, which is what a pilot's second user needs. A guest has no
        # business on a tenant somebody pays for: one mistyped id would show them a customer's
        # watchlist. A pilot or design-partner link to that tenant is still allowed.
        raise Rejected(f"tenant {req['tenant_id']!r} is a customer's (a purchase or a trial); a "
                       "friends-family guest is only linked to a demo or onboarding tenant",
                       stage="billing")
    cfg = ctx.view(req["tenant_id"])
    pre = _preflight(req, ctx)
    google = req["login"] == "google"
    logins = attach_login(req["tenant_id"], cfg["tier"],
                          email=None if google else req["email"],
                          google_email=req["email"] if google else None,
                          pool_id=None if google else ctx.pool, send_invite=req["send_invite"],
                          federated_table=ctx.fed, cognito=None if google else ctx.cognito)
    return {"tenant_id": req["tenant_id"], "tier": cfg["tier"], "modules": cfg["modules"],
            **_track(req, ctx, pre, logins, _stamp(req)), "onboard": _summary(raw.get("onboard"))}


def _get(req: dict[str, Any], ctx: _Ctx) -> dict[str, Any]:
    raw = _existing(req, ctx)
    cfg = ctx.view(req["tenant_id"])
    kinds = [kind for kind, _key, _row in _tracked(ctx, req["tenant_id"])]
    return {"tenant_id": req["tenant_id"], "tier": cfg["tier"], "modules": cfg["modules"],
            "user": "none", "onboard": _summary(raw.get("onboard")), "plane": cfg["plane"],
            "onboarded": tc.is_onboarded(raw), "billing_managed": bool(raw.get("billing_managed")),
            "has_subs": bool(raw.get("subs")),
            "logins": {k: kinds.count(k) for k in LOGINS}}


def _expire(req: dict[str, Any], ctx: _Ctx) -> dict[str, Any]:
    tenant, mode = req["tenant_id"], req["mode"]
    raw = _existing(req, ctx)
    if req["email"]:
        return {**_one_login(req, ctx, raw, mode), "mode": mode}
    _needs_stamp(req, raw)
    stamp = dict(raw["onboard"])
    if not stamp.get("expired_at"):
        # only the comp lapses: the base modules. `subs` (a purchase) is not touched, so
        # effective_entitlement keeps entitling what was bought.
        stamp.update(expired_at=_now(), expired_modules=list(raw.get("modules") or []))
        ctx.tenants.update_item(
            Key={"tenant_id": tenant}, UpdateExpression="SET #m = :e, #o = :s",
            ConditionExpression="attribute_exists(#o)",
            ExpressionAttributeNames={"#m": "modules", "#o": "onboard"},
            ExpressionAttributeValues={":e": [], ":s": stamp})
    # a tenant that bought keeps its logins: they are a customer's now
    purchased = bool(raw.get("subs") or raw.get("billing_managed"))
    counts = ({"disabled": 0, "deleted": 0, "gone": 0} if purchased
              else _reverse_all(ctx, tenant, _tracked(ctx, tenant), mode))
    cfg = ctx.view(tenant)
    return {"tenant_id": tenant, "tier": cfg["tier"], "modules": cfg["modules"], "user": "none",
            "onboard": _summary(stamp), "scope": "tenant", "mode": mode,
            "logins": {**counts, "kept": purchased}}


def _remove(req: dict[str, Any], ctx: _Ctx) -> dict[str, Any]:
    tenant = req["tenant_id"]
    raw = _existing(req, ctx)
    if req["email"]:
        return _one_login(req, ctx, raw, "delete")
    _needs_stamp(req, raw)
    if raw.get("subs") or raw.get("billing_managed"):
        raise Rejected(f"tenant {tenant!r} has a purchase; its row is never deleted — use expire "
                       "to lapse the comp modules", stage="billing")
    counts = _reverse_all(ctx, tenant, _tracked(ctx, tenant), "delete")
    try:   # logins first: a failure above leaves the row, so the call can simply be repeated
        ctx.tenants.delete_item(
            Key={"tenant_id": tenant},
            ConditionExpression="attribute_exists(#o) AND attribute_not_exists(#s) "
                                "AND attribute_not_exists(#b)",
            ExpressionAttributeNames={"#o": "onboard", "#s": "subs", "#b": "billing_managed"})
    except Exception as exc:
        if not _conditional(exc):
            raise
        raise Rejected(f"tenant {tenant!r} changed while it was being removed (a purchase?); "
                       "the row was kept", stage="billing") from exc
    return {"tenant_id": tenant, "tier": str(raw.get("tier") or ""), "modules": [],
            "user": "none", "onboard": _summary(raw.get("onboard")), "scope": "tenant",
            "removed": True, "logins": {**counts, "kept": False}}


_DISPATCH = {"put": _put, "link": _link, "get": _get, "expire": _expire, "remove": _remove}


def handle(event: Any, *, table: Any | None = None, federated_table: Any | None = None,
           cognito: Any | None = None, pool_id: str | None = None) -> dict[str, Any]:
    """One onboarding request. Pure given its stores: pass fakes for ``table`` (tenant config),
    ``federated_table`` and ``cognito``; left None they resolve from the environment
    (``ONCA_TENANT_CONFIG_TABLE``, ``ONCA_FEDERATED_MAP_TABLE``, ``ONCA_USER_POOL_ID``)."""
    log: dict[str, Any] = {"fn": FUNCTION, "action": None, "tenant": None, "cohort": None,
                           "request_id": None, "outcome": "error"}
    try:
        req = _parse(event, log)
        body = _DISPATCH[req["action"]](req, _Ctx(table, federated_table, cognito, pool_id))
        log.update(outcome="ok", user=body.get("user"))
        return {"ok": True, "request_id": req["request_id"], **body}
    except Rejected as exc:
        log.update(outcome="rejected", reason=exc.stage)
        rid = event.get("request_id") if isinstance(event, dict) else None
        return {"ok": False, "request_id": rid[:64] if isinstance(rid, str) else None,
                "error": f"REJECTED: {exc}"}
    except Exception as exc:
        log["reason"] = type(exc).__name__
        raise
    finally:
        # ONE line per call (rule 8). Enums, validated ids and an e-mail hash only: never a
        # message (the Cognito guard's text names the e-mail).
        print(json.dumps(log, sort_keys=True))


def lambda_handler(event: Any, context: Any = None) -> dict[str, Any]:
    return handle(event)
