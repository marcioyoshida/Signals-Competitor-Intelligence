"""Per-sector weekly digest opt-in (owner decision 2026-10-02: "weekly digest per segment").

Each signed-in user chooses, for each sector their organization CURRENTLY licenses, whether to
receive the Monday digest for that sector — by email, to their own login email. Consent is
explicit, stored, and withdrawable (one click from every email).

Store: rows in the EXISTING ``OncaPushTable`` (``ONCA_PUSH_TABLE``; pk only) — the same table
that already holds this user's per-device push prefs (#167). No new infrastructure.

  DIGEST#<cognito sub>        sub, tenant, groups, email, sectors[], consent_at,
                              consent_version, updated_at, [withdrawn_at], [last_sent]
  DIGESTTOK#<sha256(token)>   sub, created_at, expires_at — one per SENT email. The random
                              unsubscribe token itself is never stored, only its hash.

Routes (on ``OncaPushApi``, delegated from ``push.api_handler``; CloudFront ``/api/me/*`` and
``/api/push/*`` behaviors → the HTTP API — no new CloudFront behavior):
  GET  /api/me/digest                (Cognito JWT) → licensed sectors + this user's choice
  PUT  /api/me/digest                (Cognito JWT) {sectors[], consent: true} — sectors outside
                                     the caller's current licence are REJECTED (403)
  GET  /api/push/digest/unsubscribe?t=<token>   no JWT: a confirmation page (a GET never
                                     unsubscribes — mail scanners prefetch links)
  POST /api/push/digest/unsubscribe?t=<token>   no JWT: RFC 8058 one-click (List-Unsubscribe-Post);
                                     clears every sector and records withdrawn_at

Logs never carry an email address or a token.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import secrets
import urllib.parse
from typing import Any, Callable

#: The privacy-policy effective date the consent was given under (docs/privacy.html "Vigência").
CONSENT_VERSION = "2026-10-01"
POLICY_URL = "https://onssa.org/docs/privacy.html"
MANAGE_URL = "https://onssa.org/exec"
UNSUB_PATH = "/api/push/digest/unsubscribe"
BASE_URL = "https://onssa.org"
TOKEN_TTL_DAYS = 400
PK = "DIGEST#"
TOK = "DIGESTTOK#"

#: Same labels as /v2/context.js INDUSTRY_LABELS (duplicated by convention, not imported).
SECTOR_PT = {
    "acquiring": "Adquirência", "fintech": "Fintechs", "banking": "Bancos", "insurance": "Seguros",
    "investment-banking": "Banco de investimento", "consorcio": "Consórcios",
    "asset-management": "Gestão de ativos", "wealth-management": "Wealth",
    "real-estate-funds": "FIIs", "agri-funds": "FIAGRO", "private-markets": "Private markets",
    "financial-data-analytics": "Dados & analytics", "advisory": "Advisory", "crypto": "Cripto",
    "betting": "Apostas",
}


def sector_label(slug: str) -> str:
    return SECTOR_PT.get(slug, slug)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode()).hexdigest()


def _table(table: Any | None = None) -> Any:
    if table is not None:
        return table
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["ONCA_PUSH_TABLE"])


def _modules(tenant: Any, groups: Any) -> list[str]:
    from src.dashboard.push import modules_for

    return modules_for(tenant, list(groups or []))


# ---- API ------------------------------------------------------------------------------------
def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(body, ensure_ascii=False)}


def _html(status: int, inner: str) -> dict[str, Any]:
    page = ("<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta name='robots' content='noindex'><title>Resumo semanal — Onça</title>"
            "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 16px;"
            "color:#1b1b1b;background:#fff}button{font:inherit;padding:.6rem 1.2rem;min-height:44px}"
            "a{color:#0b5cad}</style></head><body>" + inner + "</body></html>")
    return {"statusCode": status, "headers": {"content-type": "text/html; charset=utf-8",
                                              "cache-control": "no-store", "referrer-policy": "no-referrer"},
            "body": page}


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _view(row: dict[str, Any] | None, licensed: list[str], email: str | None) -> dict[str, Any]:
    row = row or {}
    return {"available": licensed,
            "sectors": [s for s in (row.get("sectors") or []) if s in licensed],
            "email": email or row.get("email"),
            "consent_at": row.get("consent_at"), "consent_version": row.get("consent_version"),
            "withdrawn_at": row.get("withdrawn_at"), "policy_url": POLICY_URL}


def api_handler(event: dict[str, Any], context: Any, *, table: Any | None = None,
                modules: Callable[[Any, Any], list[str]] | None = None,
                now: dt.datetime | None = None) -> dict[str, Any]:
    from src.dashboard.auth import _claims, identity_from_event

    path = str(event.get("rawPath") or "").rstrip("/")
    method = str(((event.get("requestContext") or {}).get("http") or {}).get("method") or "GET").upper()
    t = _table(table)
    now = now or _now()

    if path.endswith(UNSUB_PATH):
        token = str((event.get("queryStringParameters") or {}).get("t") or "")
        return unsubscribe(token, method, table=t, now=now)

    if not path.endswith("/api/me/digest"):
        return _resp(404, {"error": "not found"})
    identity = identity_from_event(event)
    if identity is None:
        return _resp(403, {"error": "forbidden"})
    licensed = sorted((modules or _modules)(identity.tenant, identity.groups))
    key = {"pk": PK + identity.sub}
    prev = t.get_item(Key=key).get("Item")
    if method == "GET":
        return _resp(200, _view(prev, licensed, identity.email))
    if method not in ("PUT", "POST"):
        return _resp(405, {"error": "method not allowed"})

    body = _body(event)
    raw = body.get("sectors")
    if not isinstance(raw, list):
        return _resp(400, {"error": "sectors[] required"})
    sectors = sorted({str(s).strip().lower() for s in raw if str(s).strip()})
    outside = [s for s in sectors if s not in licensed]
    if outside:
        return _resp(403, {"error": "sector not licensed", "sectors": outside})
    stamp = _iso(now)
    item: dict[str, Any] = {**key, "sub": identity.sub, "tenant": identity.tenant,
                            "groups": list(identity.groups or []), "sectors": sectors,
                            "updated_at": stamp}
    if prev and prev.get("last_sent"):            # a re-save never re-opens today's send
        item["last_sent"] = prev["last_sent"]
    if sectors:
        claims = _claims(event) or {}
        email = str(identity.email or "").strip()
        if not email or "@" not in email or str(claims.get("email_verified", "true")).lower() == "false":
            return _resp(400, {"error": "a verified login email is required"})
        if body.get("consent") is not True:
            return _resp(400, {"error": "explicit consent required"})
        item.update(email=email, consent_at=stamp, consent_version=CONSENT_VERSION)
    else:                                          # withdrawal: keep the record of it, not the address
        item.update(withdrawn_at=stamp,
                    **{k: prev[k] for k in ("consent_at", "consent_version") if prev and prev.get(k)})
    t.put_item(Item=item)
    return _resp(200, _view(item, licensed, identity.email))


def unsubscribe(token: str, method: str, *, table: Any, now: dt.datetime | None = None) -> dict[str, Any]:
    """One-click withdrawal by the capability token from an email. GET shows a confirmation
    form (link prefetchers must not unsubscribe anyone); POST withdraws."""
    if not token or len(token) > 200:
        return _html(400, "<h1>Link inválido</h1><p>Gerencie o resumo semanal em "
                          f"<a href='{MANAGE_URL}'>onssa.org/exec</a>.</p>")
    ptr = table.get_item(Key={"pk": TOK + token_hash(token)}).get("Item")
    row = table.get_item(Key={"pk": PK + str(ptr.get("sub"))}).get("Item") if ptr else None
    if not row:
        return _html(404, "<h1>Link expirado ou inválido</h1><p>Gerencie o resumo semanal em "
                          f"<a href='{MANAGE_URL}'>onssa.org/exec</a>.</p>")
    if method != "POST":
        q = urllib.parse.quote(token, safe="")
        return _html(200, "<h1>Cancelar o resumo semanal</h1><p>Você deixará de receber o resumo semanal "
                          "da Onça por e-mail, para todos os setores.</p>"
                          f"<form method='post' action='{UNSUB_PATH}?t={q}'>"
                          "<input type='hidden' name='List-Unsubscribe' value='One-Click'>"
                          "<button type='submit'>Cancelar inscrição</button></form>"
                          f"<p><a href='{MANAGE_URL}'>Prefiro escolher setores</a></p>")
    stamp = _iso(now or _now())
    table.update_item(Key={"pk": row["pk"]},
                      UpdateExpression="SET sectors = :e, withdrawn_at = :w, updated_at = :w REMOVE email",
                      ExpressionAttributeValues={":e": [], ":w": stamp})
    print("digest: one-click unsubscribe recorded")
    return _html(200, "<h1>Inscrição cancelada</h1><p>Você não receberá mais o resumo semanal. "
                      f"Para voltar a receber, escolha os setores em <a href='{MANAGE_URL}'>onssa.org/exec</a>.</p>")


# ---- sender (Monday, from feed_builder) -------------------------------------------------------
def opted_rows(*, table: Any | None = None) -> list[dict[str, Any]]:
    t = _table(table)
    rows, kw = [], {}
    while True:
        page = t.scan(**kw)
        rows += [it for it in page.get("Items", [])
                 if str(it.get("pk") or "").startswith(PK) and it.get("sectors")]
        if not page.get("LastEvaluatedKey"):
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return rows


def mint_token(sub: str, *, table: Any, now: dt.datetime | None = None) -> str:
    """A fresh random unsubscribe token for ONE email; only its hash is stored."""
    token = secrets.token_urlsafe(32)
    now = now or _now()
    table.put_item(Item={"pk": TOK + token_hash(token), "sub": str(sub), "created_at": _iso(now),
                         "expires_at": int((now + dt.timedelta(days=TOKEN_TTL_DAYS)).timestamp())})
    return token


RawSender = Callable[[str, str, bytes], Any]


def _default_raw_send(sender: str, to: str, raw: bytes) -> Any:
    import boto3

    client = boto3.client("ses", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return client.send_raw_email(Source=sender, Destinations=[to], RawMessage={"Data": raw})


def build_mime(sender: str, to: str, subject: str, text: str, html: str, unsub_url: str) -> bytes:
    from email import policy
    from email.message import EmailMessage

    # 998-char lines: the default 78 would RFC 2047-encode the long, space-free List-Unsubscribe
    # URL, which mail clients then can't parse as a URI.
    msg = EmailMessage(policy=policy.SMTP.clone(max_line_length=998))
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["List-Unsubscribe"] = f"<{unsub_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


def send_sector_digests(feed: dict[str, Any], *, today: str, table: Any | None = None,
                        modules: Callable[[Any, Any], list[str]] | None = None,
                        scope: Callable[[dict[str, Any], list[str]], dict[str, Any]] | None = None,
                        raw_sender: RawSender | None = None, sender: str | None = None,
                        base_url: str = BASE_URL, now: dt.datetime | None = None) -> dict[str, int]:
    """One email per opted-in user, one section per opted sector ∩ the tenant's licence AT SEND
    TIME (a lapsed sector is skipped silently). A user already sent today is skipped (the
    pipeline runs 3x on a Monday). One user's failure never stops the others."""
    from src.dashboard import weekly_digest as wd

    tally = {"users": 0, "sent": 0, "failed": 0, "already_sent": 0, "no_licence": 0, "empty": 0}
    sender = sender or wd._config("ONCA_ALERT_EMAIL_FROM")
    if not sender:
        return tally
    t = _table(table)
    modules = modules or _modules
    if scope is None:
        from src.dashboard.feed_builder import scope_feed_to_modules as scope
    send = raw_sender or _default_raw_send
    scoped_cache: dict[tuple, dict[str, Any]] = {}
    for row in opted_rows(table=t):
        tally["users"] += 1
        try:
            if str(row.get("last_sent") or "") == today:
                tally["already_sent"] += 1
                continue
            mods = sorted(modules(row.get("tenant"), row.get("groups")))
            active = [s for s in (row.get("sectors") or []) if s in mods]
            if not active or not row.get("email"):
                tally["no_licence"] += 1
                continue
            k = tuple(mods)
            if k not in scoped_cache:
                scoped_cache[k] = scope(feed, mods)
            scoped = scoped_cache[k]
            by_ind = (((scoped.get("executive") or {}).get("cso") or {}).get("weekly") or {}).get("by_industry") or {}
            parts = []
            for s in active:
                w = by_ind.get(s)                  # exact sector only — never the __all__ fallback
                if w and w.get("headline"):
                    parts.append((sector_label(s), wd.with_sector_events(w, scoped, sector=s, as_of=today)))
            if not parts:
                tally["empty"] += 1
                continue
            token = mint_token(row["sub"], table=t, now=now)
            unsub = f"{base_url}{UNSUB_PATH}?t={urllib.parse.quote(token, safe='')}"
            subject, text, html = wd.format_sector_email(parts, unsub_url=unsub, manage_url=MANAGE_URL)
            send(sender, row["email"], build_mime(sender, row["email"], subject, text, html, unsub))
            t.update_item(Key={"pk": row["pk"]}, UpdateExpression="SET last_sent = :d",
                          ExpressionAttributeValues={":d": today})
            tally["sent"] += 1
        except Exception as exc:  # one user's SES/store error never blocks the rest
            tally["failed"] += 1
            print(f"digest: sector digest failed for one user ({type(exc).__name__})")
    return tally
