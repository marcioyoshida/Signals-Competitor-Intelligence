"""Phase D — per-tenant entitlement (ADR 002 Phase D + ADR 016).

`onca-tenant-config`: tenant_id -> {tier, modules[]}. The single entitlement source
of truth: the read boundary scopes the feed/agent to `modules[]`; `tier` selects the
delivery plane. **Fail closed** — a tenant with no record (or empty modules) has NO
entitlement, so a verified-but-unprovisioned user sees nothing rather than everything.
"""
from __future__ import annotations

import os
import re
from typing import Any

VALID_TIERS = ("entry", "saas", "sovereign")

# ADR 016 — the Entry Portal carries ONLY the entry-tier verticals, at shallow
# public-filing depth. This is the single source of truth for that set (canonical
# registry slugs). An `entry` tenant may license a SUBSET of these and nothing else;
# the higher-tier industries are never fed to Entry (enforced here at provisioning
# AND at feed-build via feed_builder.derive_entry_feed — see "fork the feed").
ENTRY_INDUSTRIES = ("agri-funds", "betting", "consorcio", "crypto", "real-estate-funds")


def allowed_industries_for_tier(tier: str) -> frozenset[str] | None:
    """The industry allow-list a tier may license. Entry is capped to the entry-tier
    verticals; SaaS/Sovereign are unrestricted (None = any registered industry)."""
    return frozenset(ENTRY_INDUSTRIES) if tier == "entry" else None


# #119 (ADR 024 readiness pass, 2026-09-12): sectors thin across EVERY officer — not a display
# artifact, a genuine "don't sell this yet" signal (see docs/2026-09-11-adr-launch-readiness.md,
# "Coverage-gated GA sector list"). Recorded as excluded-until-revisited rather than left to
# surface by accident in a provisioning call. Revisit only with a named buyer + an ingestion plan.
NOT_READY_INDUSTRIES = ("closed-pension", "securitization", "private-markets")


# #187 (owner decisions 2026-09-28): Entry is PAID with a 14-day trial (self-registration
# starts the trial); SaaS is sold per module, one storefront price per ADR 024 band. A
# storefront-managed tenant's entitlement is DERIVED at read time from {trial window, paid
# module subscriptions, operator base}, so an expired trial or a lapsed subscription takes
# effect without a job. Operator-provisioned tenants (no `billing_managed`) are unchanged.
TRIAL_DAYS = 14
SAAS_BANDS = {  # ADR 024 SaaS price bands → the modules each band's price covers
    "saas_premium": ("banking", "investment-banking", "private-markets"),
    "saas_mid": ("insurance", "asset-management", "wealth-management",
                 "financial-data-analytics", "acquiring", "agri-funds"),
    "saas_entry": ("fintech", "advisory", "betting", "crypto", "consorcio", "real-estate-funds"),
}
STOREFRONT_TIERS = ("entry",) + tuple(SAAS_BANDS)
SAAS_BAND_PRICE_BRL = {"saas_premium": 8900, "saas_mid": 4900, "saas_entry": 2900}  # R$/module/month

# Curated segment combos (owner decisions 2026-10-02, ADR 024 amendment): SaaS depth, ONE
# storefront subscription per combo, a fixed R$/month below the per-module sum. They replace
# the never-implemented "15% off from the 3rd module". This table is the single source of truth:
# the storefront sends only the combo id (tier ``saas_combo``), never a module list.
COMBO_TIER = "saas_combo"
COMBOS = {
    "bancos-completo": {"name": "Bancos Completo",
                        "modules": ("banking", "investment-banking"), "price_brl": 14900},
    "pagamentos": {"name": "Pagamentos", "modules": ("fintech", "acquiring"), "price_brl": 6600},
    "patrimonio": {"name": "Patrimônio",
                   "modules": ("wealth-management", "asset-management"), "price_brl": 8300},
    "ativos-digitais-apostas": {"name": "Ativos Digitais & Apostas",
                                "modules": ("crypto", "betting"), "price_brl": 4900},
    "fundos-imobiliario-agro": {"name": "Fundos Imobiliário & Agro",
                                "modules": ("real-estate-funds", "agri-funds"), "price_brl": 6600},
}


def band_of(module: str) -> str | None:
    """The ADR 024 SaaS band whose price covers ``module`` (None if unsold)."""
    return next((b for b, mods in SAAS_BANDS.items() if module in mods), None)


def combo_list_price(combo_id: str) -> int:
    """What the combo's modules cost bought one by one (the "economize" baseline)."""
    return sum(SAAS_BAND_PRICE_BRL[band_of(m)] for m in COMBOS[combo_id]["modules"])


def _validate_combos() -> None:
    """Import-time guard: a combo may only bundle sellable SaaS modules, priced below the sum."""
    for cid, c in COMBOS.items():
        mods = c["modules"]
        if not mods or len(set(mods)) != len(mods):
            raise ValueError(f"combo {cid!r}: empty or duplicated modules")
        for m in mods:
            if m in NOT_READY_INDUSTRIES or band_of(m) is None:
                raise ValueError(f"combo {cid!r}: {m!r} is not a sellable SaaS module")
        if not 0 < c["price_brl"] < combo_list_price(cid):
            raise ValueError(f"combo {cid!r}: price must be below the per-module sum")


_validate_combos()


def combos_payload() -> dict[str, dict[str, Any]]:
    """COMBOS as the dashboard receives it (on /api/feed's ``upgrade``), so the page never
    keeps its own copy of the combo table."""
    return {cid: {"name": c["name"], "modules": list(c["modules"]), "price_brl": c["price_brl"],
                  "list_price_brl": combo_list_price(cid)} for cid, c in COMBOS.items()}


def module_allowed(storefront_tier: str, module: str) -> bool:
    """May a purchase at ``storefront_tier`` license ``module``? The buyer picks the module at
    checkout, so the band's price must actually cover it (never banking at the 2.900 price)."""
    module = str(module or "").strip().lower()
    if module in NOT_READY_INDUSTRIES:
        return False
    if storefront_tier == "entry":
        return module in ENTRY_INDUSTRIES
    return module in SAAS_BANDS.get(storefront_tier, ())


def _now() -> "Any":
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc)


def effective_entitlement(item: dict[str, Any], now: Any = None) -> dict[str, Any]:
    """Pure: the tier/modules a storefront-managed tenant holds at ``now``, plus a ``billing``
    summary for the UI. Base (operator) modules always count; a paid module counts while its
    subscription is active; trial modules count until ``trial_until``. Fails closed: a trial
    date that doesn't parse has ended."""
    import datetime as _dt

    now = now or _now()
    base = [str(m).strip().lower() for m in (item.get("modules") or []) if str(m).strip()]
    subs = item.get("subs") or {}
    active = {m: s for m, s in subs.items() if (s or {}).get("status") == "active"}
    # A combo subscription (key ``combo:<id>``) licenses several modules; a per-module one, its key.
    paid = {m for k, s in active.items() for m in _sub_modules(k, s)}
    trial_until = str(item.get("trial_until") or "")
    try:
        trial_on = bool(trial_until) and now < _dt.datetime.fromisoformat(trial_until)
    except ValueError:
        trial_on = False
    trial_mods = [str(m) for m in (item.get("trial_modules") or [])] if trial_on else []
    mods = sorted(set(base) | paid | set(trial_mods))
    base_tier = str(item.get("tier") or "entry")
    tier = ("saas" if any((s or {}).get("tier") in ("saas", COMBO_TIER) for s in active.values())
            and base_tier == "entry" else base_tier)
    if active:
        state = "active"
    elif trial_on:
        state = "trial"
    elif subs:
        state = "lapsed"
    elif trial_until:
        state = "trial_expired"
    else:
        state = "none"
    return {"tier": tier, "modules": mods,
            "billing": {"state": state, "trial_until": trial_until or None,
                        "trial_modules": [str(m) for m in (item.get("trial_modules") or [])],
                        "paid_modules": sorted(paid), "base_modules": sorted(base),
                        "lapsed_modules": sorted(m for m, s in subs.items()
                                                 if (s or {}).get("status") != "active"
                                                 and not m.startswith(_COMBO_KEY)),
                        "bands": {m: (s or {}).get("band") for m, s in subs.items()
                                  if not m.startswith(_COMBO_KEY)},
                        "combos": sorted(k[len(_COMBO_KEY):] for k in active
                                         if k.startswith(_COMBO_KEY)),
                        "lapsed_combos": sorted(k[len(_COMBO_KEY):] for k, s in subs.items()
                                                if k.startswith(_COMBO_KEY)
                                                and (s or {}).get("status") != "active")}}


_COMBO_KEY = "combo:"


def _sub_modules(key: str, sub: Any) -> list[str]:
    """The modules one subscription licenses: a combo's stored module list (what was bought;
    falls back to COMBOS for a record without one), else the per-module key itself."""
    if not key.startswith(_COMBO_KEY):
        return [key]
    mods = (sub or {}).get("modules") or COMBOS.get(key[len(_COMBO_KEY):], {}).get("modules") or ()
    return [str(m) for m in mods]


def _table(table: Any | None = None) -> Any:
    if table is not None:
        return table
    import boto3

    return boto3.resource("dynamodb").Table(
        os.environ.get("ONCA_TENANT_CONFIG_TABLE", "onca-tenant-config")
    )


def get_tenant_config(tenant_id: str | None, *, table: Any | None = None) -> dict[str, Any] | None:
    """Return {tenant_id, tier, modules} or None (unprovisioned ⇒ no entitlement)."""
    if not tenant_id:
        return None
    try:
        item = _table(table).get_item(Key={"tenant_id": str(tenant_id)}).get("Item")
    except Exception:  # pragma: no cover - fail closed on a lookup error
        return None
    if not item:
        return None
    tier = str(item.get("tier") or "saas")
    modules = [str(m).strip().lower() for m in (item.get("modules") or []) if str(m).strip()]
    billing = None
    if item.get("billing_managed"):  # #187: trial window + paid modules, derived now
        eff = effective_entitlement(item)
        tier, modules, billing = eff["tier"], eff["modules"], eff["billing"]
    return {
        "tenant_id": str(tenant_id),
        "tier": tier,
        "modules": modules,
        "billing": billing,
        # ADR 016 delivery plane: portal (Entry static) | saas (shared multi-tenant) |
        # marketplace (the SAME product in the tenant's own AWS account). tier-1 is SaaS
        # OR Marketplace — same per-tenant read boundary either way.
        "plane": str(item.get("plane") or _default_plane(str(item.get("tier") or "saas"))),
        # ADR 016 addendum Decision 3: the IAM role ARN this marketplace-plane
        # tenant's synth Lambda calls POST /resolve as — the trust-policy
        # artifact exchanged during onboarding (infra/tenant_stack.py's
        # `OncaResolveCallerRole`). None for every non-marketplace tenant.
        "resolve_caller_role_arn": item.get("resolve_caller_role_arn") or None,
        # #302 (ADR 001 §3): the tenant's own priorities, applied to its scoped feed only.
        "watchlist": [str(e) for e in item.get("watchlist") or []],
        "score_weights": {str(k): float(v) for k, v in (item.get("score_weights") or {}).items()},
        "alert_prefs": {str(k): bool(v) for k, v in (item.get("alert_prefs") or {}).items()},
        # #305: an explicit included API quota (else api_usage.quota_for derives it from modules).
        # Live 2026-11-02 this field was dropped here, so an override never took effect.
        "api_quota_tokens": int(item["api_quota_tokens"]) if item.get("api_quota_tokens") else None,
        "custom_domain": item.get("custom_domain") or None,   # #306
    }


def list_tenant_items(*, table: Any | None = None) -> list[dict[str, Any]]:
    """#288 operator view: every raw tenant-config row (plan, modules, trial/paid state). No PII
    lives in this table (e-mails are in the federated map). Paginated scan."""
    t, out, kw = _table(table), [], {}
    while True:
        page = t.scan(**kw)
        out.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return out
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# The delivery plane a tier defaults to (overridable per tenant): a tier-1 tenant is
# `saas` unless explicitly provisioned as `marketplace` (in-account).
VALID_PLANES = ("portal", "saas", "marketplace")


def _default_plane(tier: str) -> str:
    return "portal" if tier == "entry" else "saas"


def put_tenant_config(
    tenant_id: str, tier: str, modules: list[str], *, plane: str | None = None,
    table: Any | None = None, force_not_ready: bool = False,
    resolve_caller_role_arn: str | None = None, onboard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Provision/update a tenant's entitlement. Idempotent upsert.

    ``onboard`` (#379) is the operator-onboarding stamp ``src.dashboard.onboard`` writes; it goes
    in the same put as the row, so a tenant is never left created-but-unstamped. Omitted (every
    other caller), an existing stamp is carried over like the billing fields."""
    tier = str(tier)
    if tier not in VALID_TIERS:
        raise ValueError(f"tier must be one of {VALID_TIERS}, got {tier!r}")
    plane = str(plane) if plane else _default_plane(tier)
    if plane not in VALID_PLANES:
        raise ValueError(f"plane must be one of {VALID_PLANES}, got {plane!r}")
    mods = sorted({str(m).strip().lower() for m in (modules or []) if str(m).strip()})
    # Entry tenants are capped to the entry-tier verticals (ADR 016): a higher-tier
    # industry must never enter an Entry tenant's entitlement, so the Entry dashboard
    # can never surface it. Reject rather than silently drop — a mis-scoped provision
    # is an operator error worth surfacing.
    allowed = allowed_industries_for_tier(tier)
    if allowed is not None:
        bad = [m for m in mods if m not in allowed]
        if bad:
            raise ValueError(
                f"tier {tier!r} may only license entry-tier industries "
                f"{sorted(allowed)}; got disallowed {bad}"
            )
    # #119: not-ready sectors are excluded from EVERY tier by default, not just Entry — an
    # operator provisioning a SaaS design partner is exactly the scenario this guards against,
    # since SaaS/Sovereign have no allow-list otherwise. `force_not_ready=True` is the deliberate
    # escape hatch for a named buyer with an explicit ingestion plan (see NOT_READY_INDUSTRIES).
    if not force_not_ready:
        not_ready = [m for m in mods if m in NOT_READY_INDUSTRIES]
        if not_ready:
            raise ValueError(
                f"{sorted(not_ready)} are not launch-ready (issue #119) — pass "
                "force_not_ready=True to override for a named buyer with an ingestion plan"
            )
    if resolve_caller_role_arn and not re.match(
        r"^arn:aws:iam::\d{12}:role/.+$", str(resolve_caller_role_arn)
    ):
        raise ValueError(
            f"resolve_caller_role_arn must be an IAM role ARN "
            f"(arn:aws:iam::<account>:role/<name>), got {resolve_caller_role_arn!r}"
        )
    item: dict[str, Any] = {
        "tenant_id": str(tenant_id), "tier": tier, "modules": mods, "plane": plane,
    }
    # DynamoDB's Table resource rejects a bare `None` attribute value — omit
    # the key entirely rather than write a NULL, same convention the rest of
    # this module already follows for optional fields.
    if resolve_caller_role_arn:
        item["resolve_caller_role_arn"] = str(resolve_caller_role_arn)
    if onboard:
        item["onboard"] = dict(onboard)
    # #187: an operator re-put replaces the whole item. It must not wipe what the storefront
    # manages (paid subscriptions, the trial), or a paying customer silently loses access.
    try:
        prior = _table(table).get_item(Key={"tenant_id": str(tenant_id)}).get("Item") or {}
    except Exception:  # pragma: no cover - a read failure must not block provisioning
        prior = {}
    # Same for the tenant's security policy (#164 idle limit, #257 app-lock ceiling): a re-put
    # that silently dropped it would loosen the customer's settings without anyone noticing.
    for k in _BILLING_FIELDS + _POLICY_FIELDS + _PREF_FIELDS + _ONBOARD_FIELDS:
        if k in prior and k not in item:
            item[k] = prior[k]
    _table(table).put_item(Item=item)
    return dict(item)


_BILLING_FIELDS = ("billing_managed", "subs", "trial_until", "trial_modules")
_POLICY_FIELDS = ("session_idle_days", "app_lock_minutes")
_PREF_FIELDS = ("watchlist", "score_weights", "alert_prefs", "prefs_updated_at",
                "custom_domain", "api_quota_tokens",   # #306: a SaaS tenant's own domain (scripts/provision_tenant_domain.py)
                "model_admins",   # #347: who may connect the tenant's own model (BYOK)
                # #350: the external id the tenant's own Bedrock role trusts. Losing it on a
                # re-put would break the client's trust policy, so it is carried like a pref.
                "model_external_id")
# #379: the operator-onboarding stamp {cohort, expires_on, by, at, request_id}. A re-put that
# dropped it would turn a comp tenant into one the reports count as a customer, and one that
# `onboard` expire/remove refuses to touch.
_ONBOARD_FIELDS = ("onboard",)


def is_onboarded(item: dict[str, Any] | None) -> bool:
    """#379: was this RAW tenant-config row created by operator onboarding (a friends-and-family,
    pilot or design-partner comp)? Usage and customer reports use it to leave such tenants out.
    Takes the raw item: ``get_tenant_config`` returns a derived view without the stamp."""
    return bool((item or {}).get("onboard"))


def set_tenant_prefs(tenant_id: str, *, watchlist: list[str] | None = None,
                     score_weights: dict[str, float] | None = None,
                     alert_prefs: dict[str, bool] | None = None,
                     table: Any | None = None) -> dict[str, Any]:
    """#302: write ONLY the preference fields given (already validated by ``watchlist.clean_*``)
    on an EXISTING tenant row. An update, never a put, so entitlement and billing are untouched.
    Raises KeyError for an unknown tenant."""
    from decimal import Decimal

    sets, vals = ["prefs_updated_at = :t"], {":t": _now().isoformat()}
    if watchlist is not None:
        sets.append("watchlist = :w"); vals[":w"] = list(watchlist)
    if score_weights is not None:
        sets.append("score_weights = :s"); vals[":s"] = {k: Decimal(str(v)) for k, v in score_weights.items()}
    if alert_prefs is not None:
        sets.append("alert_prefs = :a"); vals[":a"] = dict(alert_prefs)
    try:
        r = _table(table).update_item(
            Key={"tenant_id": str(tenant_id)}, UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_exists(tenant_id)", ExpressionAttributeValues=vals,
            ReturnValues="ALL_NEW")
    except Exception as exc:
        if "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc):
            raise KeyError(tenant_id) from None
        raise
    item = r.get("Attributes") or {}
    return {"watchlist": list(item.get("watchlist") or []),
            "score_weights": {k: float(v) for k, v in (item.get("score_weights") or {}).items()},
            "alert_prefs": dict(item.get("alert_prefs") or {})}


def set_model_admins(tenant_id: str, emails: list[str], *, table: Any | None = None) -> list[str]:
    """#347: the e-mails allowed to connect, test or remove the tenant's own model key. Set by an
    operator (contract-level: connecting sends the tenant's questions to a third party). An
    update on an EXISTING row; raises KeyError for an unknown tenant, ValueError for a bad e-mail."""
    clean = sorted({str(e).strip().lower() for e in emails or [] if str(e).strip()})
    bad = [e for e in clean if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e)]
    if bad:
        raise ValueError(f"not an e-mail: {bad}")
    try:
        _table(table).update_item(
            Key={"tenant_id": str(tenant_id)}, UpdateExpression="SET model_admins = :m",
            ConditionExpression="attribute_exists(tenant_id)", ExpressionAttributeValues={":m": clean})
    except Exception as exc:
        if "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc):
            raise KeyError(tenant_id) from None
        raise
    return clean


def ensure_model_external_id(tenant_id: str, *, table: Any | None = None) -> str:
    """#350: the tenant's external id for its own Bedrock role, created once and never changed
    (the client pastes it into its role's trust policy). Generated by Onça, unique per tenant:
    AWS's confused-deputy guard. Raises KeyError for an unknown tenant."""
    import secrets

    fresh = "onca-" + secrets.token_hex(16)
    try:
        r = _table(table).update_item(
            Key={"tenant_id": str(tenant_id)},
            UpdateExpression="SET model_external_id = if_not_exists(model_external_id, :x)",
            ConditionExpression="attribute_exists(tenant_id)",
            ExpressionAttributeValues={":x": fresh}, ReturnValues="UPDATED_NEW")
    except Exception as exc:
        if "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc):
            raise KeyError(tenant_id) from None
        raise
    return str((r.get("Attributes") or {}).get("model_external_id") or fresh)


def is_model_admin(cfg: dict[str, Any] | None, email: str | None) -> bool:
    """#347: may this signed-in user manage the tenant's own model key?"""
    e = str(email or "").strip().lower()
    return bool(e) and e in {str(x).strip().lower() for x in (cfg or {}).get("model_admins") or []}


def apply_purchase(tenant_id: str, storefront_tier: str, module: str, action: str, *,
                   event_id: str, table: Any | None = None) -> dict[str, Any]:
    """Grant or revoke ONE paid module subscription on an existing tenant (#187, called by the
    storefront dispatch via ``src.dashboard.upgrade``). ``action`` is "grant" or "revoke". A
    revoke marks the module lapsed rather than deleting it (history stays; re-buying restores).
    Raises KeyError for an unknown tenant, ValueError for a module the tier doesn't cover."""
    if action not in ("grant", "revoke"):
        raise ValueError("action must be grant or revoke")
    if storefront_tier not in STOREFRONT_TIERS or not module_allowed(storefront_tier, module):
        raise ValueError(f"{storefront_tier!r} does not cover module {module!r}")
    module = str(module).strip().lower()
    return _put_sub(tenant_id, module, {"tier": "entry" if storefront_tier == "entry" else "saas",
                                        "band": storefront_tier}, action, event_id, table)


def apply_combo_purchase(tenant_id: str, combo: str, action: str, *, event_id: str,
                         table: Any | None = None) -> dict[str, Any]:
    """Grant or revoke ONE combo subscription (``subs["combo:<id>"]``), sibling of
    ``apply_purchase``. The modules come from COMBOS, never from the caller. A combo is its own
    subscription entry, so revoking it removes only modules no other active subscription, base
    module or trial still covers. Raises KeyError for an unknown tenant, ValueError for an
    unknown combo."""
    if action not in ("grant", "revoke"):
        raise ValueError("action must be grant or revoke")
    combo = str(combo or "").strip().lower()
    if combo not in COMBOS:
        raise ValueError(f"unknown combo {combo!r}")
    return _put_sub(tenant_id, _COMBO_KEY + combo,
                    {"tier": COMBO_TIER, "combo": combo, "modules": list(COMBOS[combo]["modules"])},
                    action, event_id, table)


def _put_sub(tenant_id: str, key: str, fields: dict[str, Any], action: str, event_id: str,
             table: Any | None) -> dict[str, Any]:
    t = _table(table)
    item = t.get_item(Key={"tenant_id": str(tenant_id)}).get("Item")
    if not item or str(tenant_id).startswith("BILLING#"):
        raise KeyError("no such tenant")
    subs = dict(item.get("subs") or {})
    subs[key] = {**fields, "status": "active" if action == "grant" else "lapsed",
                 "event_id": str(event_id), "updated_at": _now().isoformat(timespec="seconds")}
    item["subs"] = subs
    item["billing_managed"] = True
    t.put_item(Item=item)
    return effective_entitlement(item)


def _refuse_relink(existing: dict[str, Any], email: str, tenant_id: str) -> str | None:
    """The `custom:tenant` an existing Cognito user carries (None if it has none). Raises
    ValueError when that is a DIFFERENT tenant: the attribute is immutable."""
    cur_tenant = next(
        (a["Value"] for a in existing.get("UserAttributes", []) if a["Name"] == "custom:tenant"),
        None,
    )
    if cur_tenant and cur_tenant != str(tenant_id):
        raise ValueError(
            f"{email!r} is already linked to tenant {cur_tenant!r}, not {tenant_id!r} — "
            "custom:tenant is immutable, use a different email or a new user"
        )
    return cur_tenant


def cognito_user_link(
    pool_id: str, email: str, tenant_id: str, *, client: Any | None = None,
) -> dict[str, Any] | None:
    """#379: read-only twin of ``cognito_upsert_user``'s guard, so a caller can refuse BEFORE it
    writes anything. None when no such user; else ``{"username", "tenant", "status", "enabled"}``
    (``username`` is Cognito's own opaque id, not the e-mail; ``tenant`` is None for a user
    created without `custom:tenant`). Raises the same ValueError as the upsert when the e-mail
    belongs to another tenant."""
    if client is None:
        import boto3

        client = boto3.client("cognito-idp")
    try:
        existing = client.admin_get_user(UserPoolId=pool_id, Username=email)
    except client.exceptions.UserNotFoundException:
        return None
    return {"username": str(existing.get("Username") or email),
            "tenant": _refuse_relink(existing, email, tenant_id),
            "status": str(existing.get("UserStatus") or ""),
            "enabled": bool(existing.get("Enabled", True))}


def cognito_upsert_user(
    pool_id: str, email: str, tenant_id: str, tier: str, *, client: Any | None = None,
    send_invite: bool = True,
) -> str:
    """Create/update the Cognito user that carries `custom:tenant`/`custom:tier` for a real
    tenant — closing the gap where only 4 hardcoded demo tenants get a Cognito user
    (infra/app.py's deploy-time seed); a real design partner had no scripted path at all.

    `custom:tenant` is IMMUTABLE (infra/app.py's UserPool definition), so this refuses to
    silently relink an existing email to a different tenant — that would either fail at the
    API call or, worse, look like it worked while attaching the wrong identity. Returns
    "created" or "updated" (tier only, the one mutable field — e.g. an entry→saas upgrade).

    ``send_invite`` (#379) only matters when the user is created: True (the default, what every
    existing caller gets) lets Cognito e-mail its invitation; False creates the user with the
    invitation suppressed (`MessageAction=SUPPRESS`), for a caller that sends its own message."""
    if client is None:
        import boto3

        client = boto3.client("cognito-idp")
    try:
        existing = client.admin_get_user(UserPoolId=pool_id, Username=email)
    except client.exceptions.UserNotFoundException:
        delivery: dict[str, Any] = ({"DesiredDeliveryMediums": ["EMAIL"]} if send_invite
                                    else {"MessageAction": "SUPPRESS"})
        client.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:tenant", "Value": str(tenant_id)},
                {"Name": "custom:tier", "Value": str(tier)},
            ],
            **delivery,
        )
        return "created"
    _refuse_relink(existing, email, tenant_id)
    client.admin_update_user_attributes(
        UserPoolId=pool_id, Username=email,
        UserAttributes=[{"Name": "custom:tier", "Value": str(tier)}],
    )
    return "updated"


def _federated_table(table: Any | None = None) -> Any:
    if table is not None:
        return table
    import boto3

    return boto3.resource("dynamodb").Table(
        os.environ.get("ONCA_FEDERATED_MAP_TABLE", "onca-federated-tenant-map")
    )


def map_federated_email(
    email: str, tenant_id: str, tier: str, *, table: Any | None = None,
) -> dict[str, Any]:
    """Register the tenant a Google login for `email` should resolve to (Google OAuth,
    ported from Bluefin's ADR 0017 pattern — see docs/google-oauth-runbook.md).

    Onça never auto-provisions from a federated login the way Bluefin does: Cognito
    creates a Google-authenticated user internally with no `custom:tenant` attribute,
    and that attribute is `mutable=False` — confirmed live in the Bluefin fork,
    `AdminUpdateUserAttributes` raises even when the attribute was never set at all, so
    there is no API call that can ever write it after the fact. The tenant therefore
    lives here instead, keyed by email (known to the operator BEFORE the person's first
    Google login, unlike a Google `sub`), and is injected into every token via
    `claimsOverrideDetails` by `lambda_pretoken.py` — read identically to a real
    attribute by every downstream JWT-authorized API.

    An email with no row here is not an error at login time — the pre-token trigger
    just issues a token with no `custom:tenant` claim, and the existing per-tenant read
    boundary (feed_api.py) already 403s on that. This call is what PREVENTS that outcome
    for an invited design partner, run once per person when they're onboarded (alongside
    `cognito_upsert_user` for their password-login option, if any)."""
    if tier not in VALID_TIERS:
        raise ValueError(f"unknown tier {tier!r}, expected one of {VALID_TIERS}")
    email = email.strip().lower()
    if not email:
        raise ValueError("email is required")
    _federated_table(table).put_item(
        Item={"email": email, "tenant_id": str(tenant_id), "tier": tier}
    )
    return {"email": email, "tenant_id": str(tenant_id), "tier": tier}


def _allocate_entry_tenant_id(*, table: Any | None = None) -> str:
    """An OPAQUE id (#202): the tenant id is a key in usage/billing rows, the Stripe ``ref``,
    logs and URLs, so it must never carry any part of the user's email."""
    import secrets

    for _ in range(6):
        candidate = f"entry-{secrets.token_hex(6)}"
        if get_tenant_config(candidate, table=table) is None:
            return candidate
    raise RuntimeError("could not allocate a unique entry tenant id")  # pragma: no cover


def self_register_entry_tenant(
    email: str, industries: list[str], *, table: Any | None = None,
    federated_table: Any | None = None,
) -> dict[str, Any]:
    """Lazy self-registration for a first-time Google login: the ONE write path where an
    UNPROVISIONED identity creates its own tenant. #187 (owner, 2026-09-28): Entry is paid, so
    this starts a **14-day trial** of exactly **one** entry-tier sector (ADR 024: Entry is one
    vertical per tenant); after the trial, access needs an Entry subscription bought on the
    storefront (`apply_purchase`). It still cannot escalate past the entry tier: the pick is
    checked against `ENTRY_INDUSTRIES` server-side, and the tenant_id is freshly allocated,
    never caller-supplied, so this can't attach to (or overwrite) an existing tenant."""
    import datetime as _dt

    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    picked = sorted({
        str(i).strip().lower() for i in (industries or [])
        if str(i).strip().lower() in ENTRY_INDUSTRIES
    })
    if len(picked) != 1:
        raise ValueError(f"pick exactly one entry-tier sector: {sorted(ENTRY_INDUSTRIES)}")
    tenant_id = _allocate_entry_tenant_id(table=table)
    until = (_now() + _dt.timedelta(days=TRIAL_DAYS)).isoformat(timespec="seconds")
    item = {"tenant_id": tenant_id, "tier": "entry", "modules": [], "plane": "portal",
            "billing_managed": True, "trial_until": until, "trial_modules": picked}
    _table(table).put_item(Item=item)
    map_federated_email(email, tenant_id, "entry", table=federated_table)
    return {"tenant_id": tenant_id, "tier": "entry", "modules": picked, "plane": "portal",
            "billing": effective_entitlement(item)["billing"]}


def cognito_grant_industry_group(
    pool_id: str, email: str, industry: str, *, client: Any | None = None,
) -> str:
    """Add `email` to the Cognito group named `industry` — the lightweight, no-
    tenant-config-row entitlement path (see auth.industry_groups): a single Cognito
    group membership scopes that person to exactly that industry's dashboard/feed/
    ask, no `custom:tenant`/`custom:tier` attributes or DynamoDB row required.

    Creates the Cognito user first (sending Cognito's own invite email) if they don't
    already exist — WITHOUT `custom:tenant`/`custom:tier`, since group membership
    alone is the entitlement here. Validates `industry` against the canonical
    taxonomy so a typo can't silently create a group nothing ever matches (the group
    itself must already exist — see infra/app.py's per-industry CfnUserPoolGroup —
    admin_add_user_to_group raises on an unknown group name).
    Returns "created" (new user) or "granted" (existing user, group added)."""
    from src.synth.entity_registry import INDUSTRIES

    industry = industry.strip().lower()
    if industry not in INDUSTRIES:
        raise ValueError(f"{industry!r} is not a known industry slug: {sorted(INDUSTRIES)}")
    if client is None:
        import boto3

        client = boto3.client("cognito-idp")
    outcome = "granted"
    try:
        client.admin_get_user(UserPoolId=pool_id, Username=email)
    except client.exceptions.UserNotFoundException:
        client.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
        outcome = "created"
    client.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName=industry)
    return outcome


def entitled(config: dict[str, Any] | None, industries: Any) -> bool:
    """True iff any of `industries` is in the tenant's modules. Empty modules ⇒ False."""
    mods = set((config or {}).get("modules") or [])
    if not mods:
        return False
    return bool(mods & {str(i).strip().lower() for i in (industries or [])})
