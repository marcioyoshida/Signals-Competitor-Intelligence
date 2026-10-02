"""Content-free push alerts for alert-class events — #167.

Policy (applies to every officer, every tenant): **a push carries no finding content.** The
notification text is fixed ("Novo alerta no seu painel Onça" + a count); the finding is only
visible after the app opens, i.e. after auth. A tenant opt-in for headlines would be a separate,
explicit decision — not a default.

Pieces:
* ``api_handler`` — Cognito-JWT routes ``GET /api/me/push`` (public key + this device's prefs),
  ``POST /api/me/push/subscribe`` and ``/unsubscribe`` (logout removes the device's subscription),
  and two UNAUTHENTICATED service-worker routes keyed by the subscription endpoint (an unguessable
  capability URL): ``POST /api/push/pending`` returns only a count and clears it,
  ``POST /api/push/opened`` counts a tap.
* ``notifier_handler`` — hourly (07–20 BRT). For each subscription it scopes the published feed
  EXACTLY as ``/api/feed`` does for that user (tenant modules / industry groups, fail closed),
  picks the alert-class events for the subscriber's officer and opted-in types, and pushes once
  per run when there is anything new — subject to quiet hours and a daily cap (overflow rolls into
  the next push's count). 404/410 from the push service prunes the subscription; three failures in
  a row prune it too. Every send/delivered/opened is counted per day for the operator view.

Alert-class sources (all EXISTING feed fields — nothing new is derived here):
  radar       product_radar.alerts           (App Store outage / complaint spike, z ≥ 3)
  distress    distress                        (RJ / falência / distress records)
  regulatory  sector_events critical|high     (sector-wide normative acts, #177)
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
from typing import Any

TYPES = ("radar", "distress", "regulatory")
OFFICER_TYPES = {"cso": list(TYPES), "cpo": ["radar"], "cco": ["distress"], "cro": ["regulatory"]}
DEFAULT_QUIET = (21, 7)          # BRT hours [start, end)
DEFAULT_DAILY_CAP = 3
MAX_FAILS = 3
BRT = dt.timezone(dt.timedelta(hours=-3))
TARGET = "/exec#hoje-alertas"


# ---- store ----------------------------------------------------------------------------------
def _table(table: Any | None = None) -> Any:
    if table is not None:
        return table
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["ONCA_PUSH_TABLE"])


def sub_key(endpoint: str) -> str:
    return "SUB#" + hashlib.sha256(str(endpoint).encode()).hexdigest()[:40]


def _today(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(BRT)).astimezone(BRT).date().isoformat()


def _bump(stat: str, n: int = 1, *, table: Any | None = None, now: dt.datetime | None = None) -> None:
    try:
        _table(table).update_item(Key={"pk": f"STATS#{_today(now)}"},
                                  UpdateExpression="ADD #s :n", ExpressionAttributeNames={"#s": stat},
                                  ExpressionAttributeValues={":n": n})
    except Exception as exc:  # pragma: no cover - counters are best-effort
        print(f"Warning: push stat {stat} not counted: {exc}")


def stats(days: int = 28, *, table: Any | None = None) -> dict[str, Any]:
    """Operator view: per-day sent/delivered/failed/pruned/opened + live subscriptions."""
    t = _table(table)
    rows, subs, last = [], 0, None
    kw: dict[str, Any] = {}
    while True:
        page = t.scan(**kw)
        for it in page.get("Items", []):
            pk = str(it.get("pk") or "")
            if pk.startswith("SUB#"):
                subs += 1
            elif pk.startswith("STATS#"):
                rows.append({"date": pk[6:], **{k: int(v) for k, v in it.items() if k != "pk"}})
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kw["ExclusiveStartKey"] = last
    rows.sort(key=lambda r: r["date"], reverse=True)
    return {"subscriptions": subs, "days": rows[:days]}


# ---- scoping (identical to /api/feed) -------------------------------------------------------
def modules_for(tenant: str | None, groups: list[str] | None) -> list[str]:
    from src.dashboard.tenant_config import get_tenant_config

    mods: list[str] = []
    if tenant:
        mods = list((get_tenant_config(tenant) or {}).get("modules") or [])
    if not mods and groups:
        from src.synth.entity_registry import INDUSTRIES

        mods = sorted({g.strip().lower() for g in groups if g.strip().lower() in INDUSTRIES})
    return mods


def alert_events(scoped: dict[str, Any], types: list[str], *, now: dt.date | None = None,
                 window_days: int = 3) -> list[tuple[str, str]]:
    """(type, event_id) for every alert-class event in an ALREADY-SCOPED feed, recent only."""
    floor = ((now or dt.date.today()) - dt.timedelta(days=window_days)).isoformat()
    out: list[tuple[str, str]] = []
    if "radar" in types:
        for a in ((scoped.get("product_radar") or {}).get("alerts") or []):
            if str(a.get("date") or "") >= floor and a.get("id"):
                out.append(("radar", str(a["id"])))
    if "distress" in types:
        for d in scoped.get("distress") or []:
            key = d.get("id") or f"{d.get('entity')}:{d.get('date')}:{d.get('event') or d.get('kind')}"
            if str(d.get("date") or "") >= floor:
                out.append(("distress", str(key)))
    if "regulatory" in types:
        for e in scoped.get("sector_events") or []:
            if e.get("severity") in ("critical", "high") and str(e.get("date") or "") >= floor and e.get("id"):
                out.append(("regulatory", str(e["id"])))
    return out


def in_quiet(now: dt.datetime, quiet: tuple[int, int] = DEFAULT_QUIET) -> bool:
    h = now.astimezone(BRT).hour
    start, end = quiet
    return (start <= h or h < end) if start > end else (start <= h < end)


# ---- API --------------------------------------------------------------------------------------
def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(body, ensure_ascii=False)}


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _public_key() -> str:
    from src.dashboard import webpush

    return base64.urlsafe_b64encode(webpush.public_key(_private_key())).rstrip(b"=").decode()


_PRIV: int | None = None


def _private_key() -> int:
    global _PRIV
    if _PRIV is None:
        import boto3

        from src.dashboard import webpush

        val = boto3.client("ssm").get_parameter(Name=os.environ["ONCA_VAPID_PARAM"],
                                                WithDecryption=True)["Parameter"]["Value"]
        _PRIV = webpush.load_private_key(val)
    return _PRIV


def _clean_prefs(p: dict[str, Any], officer: str) -> dict[str, Any]:
    types = [t for t in (p.get("types") or OFFICER_TYPES.get(officer, list(TYPES))) if t in TYPES]
    try:
        cap = max(1, min(10, int(p.get("daily_cap") or DEFAULT_DAILY_CAP)))
    except (TypeError, ValueError):
        cap = DEFAULT_DAILY_CAP
    q = p.get("quiet") or list(DEFAULT_QUIET)
    try:
        quiet = [int(q[0]) % 24, int(q[1]) % 24]
    except (TypeError, ValueError, IndexError):
        quiet = list(DEFAULT_QUIET)
    return {"types": types, "daily_cap": cap, "quiet": quiet}


def api_handler(event: dict[str, Any], context: Any, *, table: Any | None = None) -> dict[str, Any]:
    from src.dashboard.auth import identity_from_event

    path = str(event.get("rawPath") or "").rstrip("/")
    method = str(((event.get("requestContext") or {}).get("http") or {}).get("method") or "POST").upper()
    t = _table(table)
    if "/digest" in path:   # per-sector weekly digest opt-in shares this Lambda + table
        from src.dashboard import digest_optin

        return digest_optin.api_handler(event, context, table=t)
    body = _body(event)

    # --- service-worker routes (no JWT: the endpoint URL is the capability) ---
    if path.endswith("/api/push/pending") or path.endswith("/api/push/opened"):
        ep = str(body.get("endpoint") or "")
        if not ep.startswith("https://"):
            return _resp(400, {"error": "endpoint required"})
        key = {"pk": sub_key(ep)}
        if path.endswith("/opened"):
            if t.get_item(Key=key).get("Item"):
                _bump("opened", table=t)
            return _resp(200, {"ok": True})
        it = t.get_item(Key=key).get("Item")
        if not it:
            return _resp(200, {"count": 0, "target": TARGET})
        t.update_item(Key=key, UpdateExpression="SET pending = :z", ExpressionAttributeValues={":z": 0})
        return _resp(200, {"count": int(it.get("last_count") or 0), "target": TARGET})

    identity = identity_from_event(event)
    if identity is None:
        return _resp(403, {"error": "forbidden"})

    if method == "GET":
        ep = ""
        qs = event.get("queryStringParameters") or {}
        ep = str(qs.get("endpoint") or "")
        it = t.get_item(Key={"pk": sub_key(ep)}).get("Item") if ep else None
        mine = it if it and it.get("user") == identity.sub else None
        return _resp(200, {"public_key": _public_key(), "types": list(TYPES),
                           "subscribed": bool(mine), "prefs": (mine or {}).get("prefs")})

    ep = str(((body.get("subscription") or {}).get("endpoint")) or body.get("endpoint") or "")
    if not ep.startswith("https://"):
        return _resp(400, {"error": "subscription endpoint required"})
    key = {"pk": sub_key(ep)}
    if path.endswith("/unsubscribe"):
        it = t.get_item(Key=key).get("Item")
        if it and it.get("user") == identity.sub:
            t.delete_item(Key=key)
        return _resp(200, {"ok": True})
    if path.endswith("/subscribe"):
        officer = str(body.get("officer") or "cso").lower()
        if officer not in OFFICER_TYPES:
            officer = "cso"
        mods = modules_for(identity.tenant, identity.groups)
        if not mods:
            return _resp(403, {"error": "no entitlement"})
        item = {**key, "endpoint": ep, "user": identity.sub, "tenant": identity.tenant,
                "groups": list(identity.groups or []), "officer": officer,
                "prefs": _clean_prefs(body.get("prefs") or {}, officer),
                "seen": [], "pending": 0, "fails": 0, "sent_day": "", "sent_today": 0,
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "primed": False}
        prev = t.get_item(Key=key).get("Item")
        if prev and prev.get("user") == identity.sub:     # re-subscribe keeps history (no re-blast)
            item.update({k: prev[k] for k in ("seen", "primed", "sent_day", "sent_today") if k in prev})
        t.put_item(Item=item)
        return _resp(200, {"ok": True, "prefs": item["prefs"]})
    return _resp(404, {"error": "not found"})


# ---- notifier ---------------------------------------------------------------------------------
def notify(feed: dict[str, Any], subs: list[dict[str, Any]], *, now: dt.datetime, send: Any,
           save: Any, drop: Any, bump: Any, scope: Any, modules: Any) -> dict[str, int]:
    """One pass over every subscription. Pure orchestration — I/O is injected (tests)."""
    tally = {"sent": 0, "skipped_quiet": 0, "capped": 0, "pruned": 0, "failed": 0, "primed": 0}
    today = _today(now)
    for s in subs:
        prefs = s.get("prefs") or {}
        mods = modules(s.get("tenant"), s.get("groups"))
        if not mods:                                   # entitlement gone: stop pinging
            drop(s); tally["pruned"] += 1; bump("pruned")
            continue
        evs = alert_events(scope(feed, mods), prefs.get("types") or [], now=now.date())
        seen = set(s.get("seen") or [])
        new = [e for e in evs if f"{e[0]}:{e[1]}" not in seen]
        s["seen"] = sorted(seen | {f"{a}:{b}" for a, b in evs})[-500:]
        if not s.get("primed"):                        # first run after subscribing: no history blast
            s["primed"] = True; save(s); tally["primed"] += 1
            continue
        s["pending"] = int(s.get("pending") or 0) + len(new)
        if not s["pending"]:
            save(s)
            continue
        if s.get("sent_day") != today:
            s["sent_day"], s["sent_today"] = today, 0
        if in_quiet(now, tuple(prefs.get("quiet") or DEFAULT_QUIET)):
            save(s); tally["skipped_quiet"] += 1
            continue
        if int(s.get("sent_today") or 0) >= int(prefs.get("daily_cap") or DEFAULT_DAILY_CAP):
            save(s); tally["capped"] += 1               # rolls into tomorrow's first push
            continue
        status = send(s["endpoint"])
        bump("sent")
        if status in (404, 410):
            drop(s); tally["pruned"] += 1; bump("pruned")
            print(f"push: subscription gone ({status}), pruned {s['pk']}")
            continue
        if not 200 <= status < 300:
            s["fails"] = int(s.get("fails") or 0) + 1
            tally["failed"] += 1; bump("failed")
            if s["fails"] >= MAX_FAILS:
                drop(s); tally["pruned"] += 1; bump("pruned")
                print(f"push: {s['fails']} failures in a row ({status}), pruned {s['pk']}")
            else:
                save(s)
            continue
        bump("delivered")
        s.update(fails=0, last_count=s["pending"], pending=0,
                 sent_today=int(s.get("sent_today") or 0) + 1, last_sent=now.isoformat())
        save(s); tally["sent"] += 1
    return tally


def notifier_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    import boto3

    from src.dashboard import webpush
    from src.dashboard.feed_builder import scope_feed_to_modules

    t = _table()
    feed = json.loads(boto3.client("s3").get_object(Bucket=os.environ["ONCA_SITE_BUCKET"],
                                                    Key="feed.json")["Body"].read())
    subs, kw = [], {}
    while True:
        page = t.scan(**kw)
        subs += [it for it in page.get("Items", []) if str(it.get("pk") or "").startswith("SUB#")]
        if not page.get("LastEvaluatedKey"):
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    d = _private_key()
    subject = os.environ.get("ONCA_VAPID_SUBJECT", "mailto:contato@onssa.org")
    cache: dict[tuple, list[str]] = {}

    def modules(tenant: Any, groups: Any) -> list[str]:
        k = (tenant, tuple(groups or []))
        if k not in cache:
            cache[k] = modules_for(tenant, list(groups or []))
        return cache[k]

    def send(ep: str) -> int:
        try:
            return webpush.send(ep, d, subject=subject)
        except Exception as exc:
            print(f"push: send error {type(exc).__name__}")
            return 599

    tally = notify(feed, subs, now=dt.datetime.now(BRT), send=send,
                   save=lambda s: t.put_item(Item=s), drop=lambda s: t.delete_item(Key={"pk": s["pk"]}),
                   bump=lambda k: _bump(k, table=t), scope=scope_feed_to_modules, modules=modules)
    print(f"push notifier: {tally} over {len(subs)} subscription(s)")
    return tally
