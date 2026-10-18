"""#288 — "Uso por cliente": who is active, on what, and which trial has gone quiet.

An OPERATIONAL adoption/churn view for the operator, built from what the product already records:

* engagement events (``OncaEngagementLog``, #165/#287): ``session``, ``signin``, and the feature kinds;
  each carries the verified ``tenant`` and the user's Cognito ``sub`` as ``actor``;
* the tenant registry (``onca-tenant-config``): plan tier, modules, trial window / paid state;
* push subscriptions (one ``SUB#`` row per device, with ``tenant`` + ``user``).

ADR 021 §E: logins and page views are NOT success KPIs — nothing here feeds the ETS or the client
boards. Users appear only as counts and short one-way hashes (never an e-mail or the raw ``sub``).
Our own traffic (``internal_traffic.is_internal``) is dropped and only counted.

Operator-only: ``feed["tenant_usage"]`` is blanked in every tenant/entry/sample derivation.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import os
from collections import Counter, defaultdict
from typing import Any

from src.synth.internal_traffic import is_internal

WINDOWS = (7, 28)
# a paying or trialling tenant with no client activity for this many days is "quiet"
QUIET_DAYS = {"trial": 3, "active": 7}
TRIAL_DAYS = 14
# the per-user daily chat cap (agent_ask: ONCA_ASK_DAILY_TURNS, default 150)
CHAT_CAP = int(os.environ.get("ONCA_ASK_DAILY_TURNS") or 150)
MAX_USERS = 20
# feature kinds worth listing (session/sign-in are reported on their own)
FEATURES = {"hoje": "Hoje", "headline": "manchete", "drawer": "fontes do cartão", "tile": "indicadores",
            "why": "evidência", "source": "fonte aberta", "ask": "chat", "share": "compartilhar",
            "report": "Reportar", "nav": "troca de oficial/visão", "upgrade": "assinatura",
            "push": "alertas", "install": "instalar app"}
_STATE_PT = {"trial": "teste", "active": "pago", "lapsed": "cancelado", "trial_expired": "teste encerrado",
             "none": "provisionado", "unknown": "sem cadastro"}


def _ts(v: Any) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def user_hash(actor: Any) -> str:
    """A short one-way handle for a user: stable across builds, not reversible to the sub/e-mail."""
    return hashlib.sha256(("onca-usage:" + str(actor or "")).encode()).hexdigest()[:8]


def _plan(item: dict[str, Any] | None, now: dt.datetime) -> dict[str, Any]:
    """Plan + billing state of a tenant-config row (the storefront's effective entitlement)."""
    if not item:
        return {"tier": None, "modules": [], "state": "unknown", "trial_until": None}
    tier = str(item.get("tier") or "saas")
    mods = sorted(str(m) for m in (item.get("modules") or []))
    state, until = "none", None
    if item.get("billing_managed"):
        from src.dashboard.tenant_config import effective_entitlement

        eff = effective_entitlement(item, now)
        tier, mods, state = eff["tier"], eff["modules"], eff["billing"]["state"]
        until = eff["billing"].get("trial_until")
    return {"tier": tier, "modules": mods, "state": state, "trial_until": until}


def build(events: list[dict[str, Any]], tenants: list[dict[str, Any]] | None = None, *,
          push_by_tenant: dict[str, int] | None = None, now: dt.datetime | None = None) -> dict[str, Any]:
    """One row per client tenant (registered or seen in events), risk-sorted: a trial gone quiet
    first, then a paying tenant gone quiet, then the rest by last activity."""
    now = now or dt.datetime.now(dt.timezone.utc)
    floors = {w: now - dt.timedelta(days=w) for w in WINDOWS}
    events = [e for e in (events or []) if isinstance(e, dict)]
    internal_events = sum(1 for e in events if is_internal(e))
    events = [e for e in events if not is_internal(e)]
    registry = {str(t.get("tenant_id")): t for t in (tenants or []) if isinstance(t, dict) and t.get("tenant_id")}
    internal_tenants = sorted(t for t in registry if is_internal({"tenant": t}))
    for t in internal_tenants:
        registry.pop(t)

    by_tenant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_tenant[str(e.get("tenant") or "")].append(e)
    rows = []
    for tid in sorted(set(registry) | {t for t in by_tenant if t}):
        rows.append(_row(tid, registry.get(tid), by_tenant.get(tid, []), now, floors,
                         int((push_by_tenant or {}).get(tid) or 0)))
    untenanted = len(by_tenant.get("", []))

    def risk(r: dict[str, Any]) -> tuple:
        st, q = r["plan"]["state"], r["quiet"]
        rank = 0 if (st == "trial" and q) else 1 if (st == "active" and q) else 2 if st == "trial" else 3 if st == "active" else 4
        return (rank, r["last_activity"] or "")
    rows.sort(key=risk)
    return {
        "as_of": now.isoformat(timespec="seconds"),
        "windows": list(WINDOWS), "quiet_days": QUIET_DAYS, "chat_cap": CHAT_CAP,
        "tenants": rows,
        "n_tenants": len(rows), "n_active_7": sum(1 for r in rows if r["users"]["7"]),
        "n_quiet": sum(1 for r in rows if r["quiet"]),
        "internal_events_excluded": internal_events,
        "internal_tenants_excluded": len(internal_tenants),
        "events_without_tenant": untenanted,
        "note": "Visão operacional de adoção (ADR 021 §E): sessões e cliques não são KPI de sucesso "
                "e não entram no ETS.",
    }


def _row(tid: str, item: dict[str, Any] | None, evs: list[dict[str, Any]], now: dt.datetime,
         floors: dict[int, dt.datetime], push_users: int) -> dict[str, Any]:
    plan = _plan(item, now)
    timed = [(e, _ts(e.get("created_at"))) for e in evs]
    timed = [(e, t) for e, t in timed if t]
    last = max((t for _, t in timed), default=None)
    win = {w: [e for e, t in timed if t >= f] for w, f in floors.items()}
    w28 = win[28]
    users = {str(w): len({e.get("actor") for e in win[w] if e.get("actor")}) for w in WINDOWS}
    sessions = {str(w): sum(1 for e in win[w] if e.get("kind") == "session") for w in WINDOWS}
    devices = Counter(str(e.get("device") or "?") for e in w28 if e.get("kind") == "session")
    feats = Counter(e.get("kind") for e in w28 if e.get("kind") in FEATURES)
    asks = [(e, t) for e, t in timed if e.get("kind") == "ask" and e.get("action") == "open"]
    chat = {str(w): sum(1 for _, t in asks if t >= floors[w]) for w in WINDOWS}
    per_user_day = Counter((e.get("actor"), t.date().isoformat()) for e, t in asks if t >= floors[28])
    signin = Counter(e.get("action") for e in w28 if e.get("kind") == "signin")
    # quiet: days since the last client activity — or, never used, since the trial started
    since = last
    if since is None and plan["state"] == "trial" and plan["trial_until"]:
        end = _ts(plan["trial_until"])
        since = end - dt.timedelta(days=TRIAL_DAYS) if end else None
    idle = (now - since).days if since else None
    limit = QUIET_DAYS.get(plan["state"])
    quiet = bool(limit) and (idle is None or idle >= limit)
    trial_left = None
    if plan["state"] == "trial" and plan["trial_until"]:
        end = _ts(plan["trial_until"])
        trial_left = max(0, math.ceil((end - now).total_seconds() / 86400)) if end else None
    per_user: dict[str, dict[str, Any]] = {}
    for e, t in timed:
        h = user_hash(e.get("actor"))
        u = per_user.setdefault(h, {"user": h, "last": None, "sessions_28": 0, "events_28": 0})
        u["last"] = max(u["last"] or "", t.isoformat(timespec="seconds"))
        if t >= floors[28]:
            u["events_28"] += 1
            u["sessions_28"] += e.get("kind") == "session"
    return {
        "tenant": tid,
        "plan": {**plan, "state_pt": _STATE_PT.get(plan["state"], plan["state"]), "trial_days_left": trial_left},
        "last_activity": last.isoformat(timespec="seconds") if last else None,
        "idle_days": idle, "quiet": quiet, "no_use": not evs,
        "users": users, "sessions": sessions, "devices": dict(devices),
        "features": [{"kind": k, "label": FEATURES[k], "count": n} for k, n in feats.most_common()],
        "chat": {**chat, "peak_user_day": max(per_user_day.values(), default=0), "cap": CHAT_CAP},
        "reports_28": sum(1 for e in w28 if e.get("kind") == "report" and e.get("action") == "sent"),
        "push_users": push_users,
        "signin": dict(signin),
        "user_list": sorted(per_user.values(), key=lambda u: u["last"] or "", reverse=True)[:MAX_USERS],
    }
