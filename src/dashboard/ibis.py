"""Ibis Brazil module (#293): crypto-partner register alerts for the CCO board.

Ibis (the global register of regulated crypto entities) watches a bank's crypto partners and
sends one signed webhook per register event. Onça holds ONE Ibis API key and ONE webhook; the
tenant mapping is ours: each watch (Ibis ``watch.lookup_key``) maps to the tenants that asked
for it, and a delivery fans out to those tenants' CCO boards.

Contract: Ibis ``contracts/onca-brazil-module.md`` (pinned in tests/contracts/ibis/). Rules the
board keeps (Ibis ADR 0005, rule 1), enforced here by what we store and return:
- ``text_pt_br`` and ``disclaimer_pt_br`` are stored and shown VERBATIM. Onça never rewords a
  sentence, never derives a status from a webhook and never labels a partner (no badge, score,
  "seguro"/"regular"/"irregular"). ``event.type`` is kept for filtering only.
- A partner's status now comes from Ibis ``/v1/check``; its ``text`` is returned as Ibis wrote it
  (English today), never translated here.
- Watches are by an identifier the register prints (CNPJ, LEI, a register's number, a website),
  never by name.

Routes (one Lambda):
- ``POST /api/hooks/ibis``: the receiver. No JWT: the HMAC signature over the raw body is the
  authentication, checked before parsing; ``t`` more than 5 minutes off is refused. Writes are
  idempotent (keys derive from the delivery id), so an Ibis retry never duplicates an alert.
- ``GET /api/me/ibis``: the caller's alerts and watches. ``POST``/``DELETE /api/me/ibis/watch``
  add/remove a watch. ``GET /api/me/ibis/check``: one partner now. All need a verified tenant
  that is not Entry-tier (the CCO board is officer-suite).

Secrets live in SSM SecureString (``/onca/ibis/*``), never in this public repo, and are never
logged.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any, Callable

TOLERANCE_S = 300
KEY_PARAM = os.environ.get("ONCA_IBIS_KEY_PARAM", "/onca/ibis/api-key")
SECRET_PARAM = os.environ.get("ONCA_IBIS_SECRET_PARAM", "/onca/ibis/webhook-secret")
URL_PARAM = os.environ.get("ONCA_IBIS_URL_PARAM", "/onca/ibis/base-url")
MAX_EVENTS = 50
MAX_WATCHES_PER_TENANT = 200

# The identifiers a watch may use: the register prints them. "name" is refused by Ibis and here.
_KINDS = {
    "cnpj": re.compile(r"^\d{14}$"),
    "lei": re.compile(r"^[A-Z0-9]{18}\d{2}$"),
    "domain": re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"),
    "registration_number": re.compile(r"^[A-Za-z0-9./ -]{1,64}$"),
}
_REGISTER = re.compile(r"^[a-z]{2}-[a-z0-9-]{2,40}$")
_JURISDICTION = re.compile(r"^[A-Z]{2}$")

_cache: dict[str, str] = {}


# --- plumbing ----------------------------------------------------------------------------------

def _param(name: str) -> str:
    if name not in _cache:
        from src import secretvalue

        _cache[name] = secretvalue.read(name)
    return _cache[name]


def _table(table: Any | None = None) -> Any:
    if table is not None:
        return table
    import boto3

    return boto3.resource("dynamodb").Table(os.environ.get("ONCA_IBIS_TABLE", "onca-ibis"))


def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"content-type": "application/json",
                                              "cache-control": "no-store"},
            "body": json.dumps(body, ensure_ascii=False)}


def _headers(event: dict[str, Any]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in ((event or {}).get("headers") or {}).items()}


def _raw_body(event: dict[str, Any]) -> bytes:
    body = (event or {}).get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode("utf-8") if isinstance(body, str) else bytes(body)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- signature ---------------------------------------------------------------------------------

def sign(secret: str, body: bytes, at: int) -> str:
    """Ibis's scheme: ``t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>``."""
    mac = hmac.new(secret.encode("utf-8"), f"{at}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    return f"t={at},v1={mac}"


def verify(secret: str, body: bytes, header: str, *, now: float | None = None,
           tolerance: int = TOLERANCE_S) -> bool:
    """True only for a fresh signature by ``secret`` over exactly ``body``. Constant-time."""
    if not secret or not header:
        return False
    parts: dict[str, list[str]] = {}
    for p in header.split(","):
        if "=" in p:
            k, v = p.strip().split("=", 1)
            parts.setdefault(k, []).append(v)
    try:
        at = int(parts["t"][0])
    except (KeyError, ValueError, IndexError):
        return False
    if abs((now if now is not None else time.time()) - at) > tolerance:
        return False
    want = sign(secret, body, at).split("v1=", 1)[1]
    return any(hmac.compare_digest(want, v) for v in parts.get("v1", []))


# --- receiver ----------------------------------------------------------------------------------

def _alert_item(tenant: str, ev: dict[str, Any], raw: str, received_at: str) -> dict[str, Any]:
    e, entry = ev.get("event") or {}, ev.get("entry") or {}
    return {
        "pk": f"tenant#{tenant}",
        "sk": f"evt#{e.get('detected_at') or received_at}#{ev['delivery_id']}",
        "delivery_id": ev["delivery_id"],
        "lookup_key": (ev.get("watch") or {}).get("lookup_key"),
        # verbatim, as Ibis wrote them (rule 1): never edited or derived
        "text_pt_br": ev.get("text_pt_br"),
        "disclaimer_pt_br": ev.get("disclaimer_pt_br"),
        "text": ev.get("text"),
        "disclaimer": ev.get("disclaimer"),
        "entry": {k: entry.get(k) for k in ("url", "register", "regulator", "name_as_published",
                                            "jurisdiction", "register_id")},
        "event": {"type": e.get("type"), "detected_at": e.get("detected_at"),
                  "evidence": list(e.get("evidence") or []), "event_id": e.get("event_id")},
        "received_at": received_at,
        "raw": raw,   # the signed delivery as received: the evidence of what Ibis said
    }


def receive(event: dict[str, Any], *, table: Any | None = None,
            secret: str | None = None, now: float | None = None) -> dict[str, Any]:
    raw = _raw_body(event)
    hdr = _headers(event)
    if not verify(secret if secret is not None else _param(SECRET_PARAM), raw,
                  hdr.get("ibis-signature", ""), now=now):
        return _resp(401, {"error": "bad signature"})
    try:
        ev = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _resp(400, {"error": "body is not JSON"})
    did = str(ev.get("delivery_id") or hdr.get("ibis-delivery-id") or "")
    if not did or not isinstance(ev, dict):
        return _resp(400, {"error": "no delivery id"})
    ev["delivery_id"] = did
    t = _table(table)
    received_at = _now_iso()
    kind = str(ev.get("type") or "")
    if kind == "ibis.test":
        t.put_item(Item={"pk": f"dlv#{did}", "sk": "-", "type": kind, "received_at": received_at})
        return _resp(200, {"ok": True, "test": True})
    if not all(k in ev for k in ("watch", "event", "entry", "text")):
        return _resp(400, {"error": "not a WebhookEvent"})
    lookup_key = str((ev.get("watch") or {}).get("lookup_key") or "")
    tenants = [str(i["sk"]).split("#", 1)[1] for i in _query(t, f"watch#{lookup_key}", "tenant#")]
    text = raw.decode("utf-8")
    # Idempotent: the same delivery writes the same keys, so a retry overwrites, never duplicates.
    for tenant in tenants:
        t.put_item(Item=_alert_item(tenant, ev, text, received_at))
    t.put_item(Item={"pk": f"dlv#{did}", "sk": "-", "type": kind, "lookup_key": lookup_key,
                     "tenants": len(tenants), "received_at": received_at,
                     **({} if tenants else {"raw": text})})   # an unmapped delivery stays auditable
    return _resp(200, {"ok": True, "tenants": len(tenants)})


def _query(t: Any, pk: str, prefix: str, *, newest_first: bool = False,
           limit: int | None = None) -> list[dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    kw: dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(prefix),
                          "ScanIndexForward": not newest_first}
    if limit:
        kw["Limit"] = limit
    return list(t.query(**kw).get("Items") or [])


# --- Ibis client ---------------------------------------------------------------------------------

def ibis_call(method: str, path: str, *, body: dict[str, Any] | None = None,
              params: dict[str, str] | None = None, timeout: float = 10.0) -> tuple[int, dict[str, Any]]:
    """One call to Ibis with Onça's server-side key. Returns (status, json)."""
    import urllib.error
    import urllib.parse
    import urllib.request

    url = _param(URL_PARAM).rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "x-api-key": _param(KEY_PARAM), "content-type": "application/json",
        "user-agent": "onca-ibis/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


Caller = Callable[..., "tuple[int, dict[str, Any]]"]


def clean_query(body: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    """The Ibis query for one watch, or an error in Portuguese. Never by name."""
    kind = str(body.get("kind") or "").strip().lower()
    value = str(body.get("value") or "").strip()
    if kind == "name":
        return None, "O Ibis não acompanha por nome: use CNPJ, LEI, número de registro ou site."
    if kind not in _KINDS:
        return None, "Tipo de identificador inválido (cnpj, lei, registration_number, domain)."
    if kind == "cnpj":
        value = re.sub(r"\D", "", value)
    elif kind == "lei":
        value = value.upper()
    elif kind == "domain":
        value = re.sub(r"^https?://", "", value.lower()).split("/", 1)[0].removeprefix("www.")
    if not _KINDS[kind].match(value):
        return None, f"Valor inválido para {kind}."
    q = {kind: value}
    if kind == "registration_number":
        reg = str(body.get("register") or "").strip().lower()
        if not _REGISTER.match(reg):
            return None, "Informe o registro (ex.: br-bcb) para um número de registro."
        q["register"] = reg
    jur = str(body.get("jurisdiction") or "").strip().upper()
    if jur:
        if not _JURISDICTION.match(jur):
            return None, "Jurisdição inválida (código de 2 letras, ex.: BR)."
        q["jurisdiction"] = jur
    return q, None


def _entitled(event: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """(tenant, None) when the caller may use the module, else (None, error response)."""
    from src.dashboard.auth import identity_from_event
    from src.dashboard.tenant_config import get_tenant_config

    ident = identity_from_event(event)
    if ident is None or not ident.tenant:
        return None, _resp(403, {"error": "forbidden"})
    cfg = get_tenant_config(ident.tenant) or {}
    if str(cfg.get("tier") or "") == "entry" or not cfg.get("modules"):
        return None, _resp(403, {"error": "O módulo de parceiros cripto faz parte da suíte de diretoria."})
    return ident.tenant, None


def add_watch(tenant: str, body: dict[str, Any], *, table: Any | None = None,
              call: Caller = ibis_call, who: str = "") -> dict[str, Any]:
    q, err = clean_query(body)
    if err:
        return _resp(400, {"error": err})
    t = _table(table)
    if len(_query(t, f"tenant#{tenant}", "watch#")) >= MAX_WATCHES_PER_TENANT:
        return _resp(400, {"error": f"Limite de {MAX_WATCHES_PER_TENANT} parceiros acompanhados."})
    status, out = call("POST", "/v1/watchlist", body={"queries": [q]})
    res = ((out or {}).get("results") or [{}])[0]
    watch = res.get("watch") or {}
    if status >= 300 or not watch.get("lookup_key") or res.get("error"):
        return _resp(502 if status >= 500 else 400,
                     {"error": res.get("error") or (out or {}).get("error") or f"Ibis respondeu {status}"})
    lk = watch["lookup_key"]
    item = {"query": q, "jurisdiction": watch.get("jurisdiction"), "added_at": _now_iso(), "added_by": who}
    t.put_item(Item={"pk": f"watch#{lk}", "sk": f"tenant#{tenant}", **item})
    t.put_item(Item={"pk": f"tenant#{tenant}", "sk": f"watch#{lk}", **item})
    # The partner's status now, as Ibis answers it (English today; never translated here).
    s2, ans = call("POST", "/v1/screen", body={"queries": [q]})
    answer = (((ans or {}).get("results") or [{}])[0]) if s2 < 300 else {}
    return _resp(200, {"ok": True, "lookup_key": lk, "answer": _answer_view(answer)})


def remove_watch(tenant: str, lookup_key: str, *, table: Any | None = None,
                 call: Caller = ibis_call) -> dict[str, Any]:
    t = _table(table)
    mine = t.get_item(Key={"pk": f"tenant#{tenant}", "sk": f"watch#{lookup_key}"}).get("Item")
    if not mine:
        return _resp(404, {"error": "Parceiro não acompanhado."})
    t.delete_item(Key={"pk": f"tenant#{tenant}", "sk": f"watch#{lookup_key}"})
    t.delete_item(Key={"pk": f"watch#{lookup_key}", "sk": f"tenant#{tenant}"})
    if not _query(t, f"watch#{lookup_key}", "tenant#"):    # last tenant out: drop Ibis's watch
        call("DELETE", "/v1/watchlist", params=dict(mine.get("query") or {}))
    return _resp(200, {"ok": True})


def _answer_view(a: dict[str, Any]) -> dict[str, Any] | None:
    """Only what Ibis wrote: its text and disclaimer verbatim, and the entry links."""
    if not a:
        return None
    ans = a.get("answer") if isinstance(a.get("answer"), dict) else a   # a ScreenResult or an Answer
    if not ans.get("text"):
        return None
    return {"text": ans.get("text"), "disclaimer": ans.get("disclaimer"),
            "generated_at": ans.get("generated_at"),
            "links": [x.get("url") for x in (ans.get("regulator_links") or [])
                      if isinstance(x, dict) and x.get("url")][:5]}


def list_for(tenant: str, *, table: Any | None = None) -> dict[str, Any]:
    t = _table(table)
    alerts = []
    for i in _query(t, f"tenant#{tenant}", "evt#", newest_first=True, limit=MAX_EVENTS):
        alerts.append({k: i.get(k) for k in ("delivery_id", "lookup_key", "text_pt_br",
                                             "disclaimer_pt_br", "text", "disclaimer", "entry",
                                             "event", "received_at")})
    watches = [{"lookup_key": str(i["sk"]).split("#", 1)[1], "query": i.get("query"),
                "jurisdiction": i.get("jurisdiction"), "added_at": i.get("added_at")}
               for i in _query(t, f"tenant#{tenant}", "watch#")]
    return _resp(200, {"alerts": alerts, "watches": watches})


# --- handler -------------------------------------------------------------------------------------

def handler(event: dict[str, Any], context: Any = None, *, table: Any | None = None,
            call: Caller | None = None) -> dict[str, Any]:
    path = str(event.get("rawPath") or "").rstrip("/")
    method = str(((event.get("requestContext") or {}).get("http") or {}).get("method") or "GET").upper()
    if path.endswith("/api/hooks/ibis"):
        if method != "POST":
            return _resp(405, {"error": "POST only"})
        return receive(event, table=table)
    tenant, err = _entitled(event)
    if err:
        return err
    call = call or ibis_call
    if path.endswith("/api/me/ibis") and method == "GET":
        return list_for(tenant, table=table)
    if path.endswith("/api/me/ibis/watch"):
        try:
            body = json.loads(_raw_body(event) or b"{}") if method != "GET" else {}
        except ValueError:
            return _resp(400, {"error": "JSON inválido"})
        if method == "POST":
            from src.dashboard.auth import identity_from_event

            who = (identity_from_event(event) or None)
            return add_watch(tenant, body if isinstance(body, dict) else {}, table=table, call=call,
                             who=getattr(who, "sub", "") or "")
        if method == "DELETE":
            lk = str((body or {}).get("lookup_key") or
                     ((event.get("queryStringParameters") or {}).get("lookup_key")) or "")
            return remove_watch(tenant, lk, table=table, call=call) if lk else \
                _resp(400, {"error": "lookup_key"})
    if path.endswith("/api/me/ibis/check") and method == "GET":
        q, qerr = clean_query(event.get("queryStringParameters") or {})
        if qerr:
            return _resp(400, {"error": qerr})
        status, out = call("GET", "/v1/check", params=q)
        if status >= 300:
            return _resp(502 if status >= 500 else 400, {"error": f"Ibis respondeu {status}"})
        return _resp(200, {"answer": _answer_view(out)})
    return _resp(404, {"error": "not found"})
