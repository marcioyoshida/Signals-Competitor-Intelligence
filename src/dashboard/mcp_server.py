"""Onça's remote MCP server (#181), the operator write surface (#182) and a minimal A2A
endpoint (#186) — plain JSON-RPC 2.0 over HTTP POST, no SDK (Tarantula ``src/agentapi/mcp.py``
pattern: three methods don't justify a dependency tree in a hand-staged Lambda asset).

Endpoints (each its own OAuth resource/audience — ``src/dashboard/oauth``):

* ``POST /mcp`` — read tools over the caller's LICENSED scope. What an LLM can't know from
  training: live Brazilian public records (CVM, BCB, CADE, DOU, CEIS/CNEP, PNCP…) resolved to a
  curated entity registry. Every payload carries ``as_of``, ``source``, ``license`` and a URL per
  row — provenance is what makes a model prefer (and cite) the tool.
* ``POST /mcp/ops`` — one tool per ``/api/act`` catalog intent, for OPERATORS (elevated Cognito
  group) only. Never in a public listing. A non-elevated token sees an empty ``tools/list``.
* ``POST /a2a`` — A2A JSON-RPC ``message/send`` → the same grounded ``ask``.

Auth: every request needs a bearer access token from Onça's own authorization server, bound to
that endpoint's audience. Without one → **401 + ``WWW-Authenticate``** pointing at the Protected
Resource Metadata, from THIS Lambda (no edge gate above it, so a client can always learn it must
sign in — the Tarantula "unreachable handshake" trap). Entitlement (licensed industries) is
recomputed from the tenant record on every call, never trusted from the token.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import time
import unicodedata
from typing import Any, Callable

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "onca-onssa", "version": "1.0.0", "title": "Onça — inteligência competitiva e regulatória (BR)"}
LICENSE = ("Dados licenciados ao seu tenant Onça; cite 'Onça (onssa.org)' e a fonte primária de cada "
           "linha. Redistribuição fora da sua organização requer autorização. https://onssa.org/docs/terms.html")
SOURCE_FEED = ("Onça: registros públicos brasileiros (CVM, BCB, CADE, DOU, CEIS/CNEP, PNCP, Receita) e "
               "imprensa especializada, resolvidos a um registro curado de entidades.")
SOURCE_REGISTRY = ("Registro curado de entidades da Onça (cadastros CVM/BCB/SUSEP/Receita + curadoria "
                   "humana com proveniência, ADR 018).")
_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
FEED_TTL_S = 300

_cache: dict[str, Any] = {}


# ---- data access (injected in tests) ------------------------------------------------------
def _norm(s: Any) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


def load_feed() -> dict[str, Any]:
    hit = _cache.get("feed")
    if hit and time.time() - hit[0] < FEED_TTL_S:
        return hit[1]
    import boto3
    raw = boto3.client("s3").get_object(Bucket=os.environ["ONCA_SITE_BUCKET"], Key="feed.json")["Body"].read()
    feed = json.loads(raw)
    _cache["feed"] = (time.time(), feed)
    return feed


def scoped_feed(modules: list[str], elevated: bool, load: Callable[[], dict] = load_feed) -> dict[str, Any]:
    from src.dashboard.feed_builder import scope_feed_to_modules

    feed = load()
    if not modules and elevated:       # an operator with no tenant modules reads everything
        modules = [i.get("slug") for i in (feed.get("industries") or []) if i.get("slug")]
    return scope_feed_to_modules(feed, modules)


def _urls(c: dict[str, Any], n: int = 5) -> list[str]:
    out = []
    for cit in c.get("citations") or c.get("sources") or []:
        u = cit.get("url") if isinstance(cit, dict) else cit
        if u and u not in out:
            out.append(u)
    return out[:n]


# ---- read tools ---------------------------------------------------------------------------
def read_tools() -> list[dict[str, Any]]:
    return [
        {"name": "lookup_entity", "title": "Resolver uma instituição brasileira no registro Onça",
         "annotations": _READ_ONLY,
         "description": (
             "Resolve a Brazilian financial institution / company by name, alias, brand or CNPJ "
             "(8-digit root) to Onça's CURATED entity registry: canonical id, display name, "
             "aliases, industries, CNPJ roots, B3 ticker, ownership. Use it before "
             "entity_signals, and whenever a name is ambiguous (e.g. 'BB', 'Inter', 'XP'). Only "
             "entities in your licensed industries are returned. Deterministic: no model in the loop."),
         "inputSchema": {"type": "object", "properties": {
             "query": {"type": "string", "description": "Name, alias, brand or CNPJ (root) — e.g. 'Itaú', '60701190'."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
             "required": ["query"], "additionalProperties": False}},
        {"name": "entity_signals", "title": "Sinais recentes de uma instituição (com fontes)",
         "annotations": _READ_ONLY,
         "description": (
             "LIVE: what happened recently to one institution — regulatory acts, sanctions and "
             "enforcement (CVM/BCB/CADE/CEIS), product and competitive moves, distress/RJ — each "
             "row dated, scored for competitive threat (0–100) and linked to its PRIMARY source. "
             "Covers events after your training cutoff. Pass the id from lookup_entity (or a "
             "name). Deterministic retrieval; the one-line summaries were written by Onça's "
             "synthesis pipeline from the cited sources."),
         "inputSchema": {"type": "object", "properties": {
             "entity": {"type": "string", "description": "Entity id from lookup_entity, or a name."},
             "days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "Look-back window, default 30."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
             "required": ["entity"], "additionalProperties": False}},
        {"name": "regulatory_events", "title": "Mudanças regulatórias setoriais (ato oficial primeiro)",
         "annotations": _READ_ONLY,
         "description": (
             "LIVE: sector-wide regulatory changes in Brazil for your licensed industries — new "
             "rules, bans, deadlines (Medidas Provisórias, Leis, Resoluções CMN/BCB/CVM/SUSEP, "
             "Portarias) — with severity, affected-entity count and the official act (DOU) plus "
             "press coverage as sources. Deterministic: no model in the loop."),
         "inputSchema": {"type": "object", "properties": {
             "industry": {"type": "string", "description": "Optional industry slug (e.g. 'banking', 'betting', 'insurance')."},
             "days": {"type": "integer", "minimum": 1, "maximum": 120}},
             "additionalProperties": False}},
        {"name": "ask", "title": "Perguntar à Onça (resposta fundamentada, com citações)",
         "annotations": _READ_ONLY,
         "description": (
             "Ask a free-form question about Brazilian financial-sector competitors and regulation "
             "and get an answer GROUNDED ONLY in Onça's licensed data, with citations. This is a "
             "LANGUAGE-MODEL call and CAN BE WRONG or decline when the data doesn't support an "
             "answer — check `citations`. Prefer the deterministic tools when they can answer."),
         "inputSchema": {"type": "object", "properties": {
             "q": {"type": "string", "description": "The question (Portuguese or English)."},
             "industry": {"type": "string", "description": "Optional industry slug to scope the answer."}},
             "required": ["q"], "additionalProperties": False}},
    ]


def _envelope(rows_key: str, rows: list[dict[str, Any]], as_of: Any, source: str, **extra: Any) -> dict[str, Any]:
    return {"as_of": as_of, "source": source, "license": LICENSE, rows_key: rows, "count": len(rows), **extra}


def lookup_entity(scoped: dict[str, Any], query: str, limit: int, *,
                  resolve: Callable[[str], list[str]], get: Callable[[str], dict | None]) -> dict[str, Any]:
    visible = scoped.get("entity_attrs") or {}
    q = str(query or "").strip()
    nq = _norm(q)
    digits = "".join(ch for ch in q if ch.isdigit())
    indexed = [e for e in resolve(q) + (resolve("cnpj:" + digits[:8]) if len(digits) >= 8 else [])
               if e in visible]
    contains = [e for e, a in visible.items()   # the registry indexes are exact only
                if nq and (nq in _norm(a.get("label")) or nq == _norm(e))]
    # Rank: exact display-name match, then CNPJ/alias index hits, then label prefix, then contains,
    # shorter names first. An alias index entry can be noisy (a fund carrying a bare manager
    # alias), so it never outranks the entity whose NAME is the query.
    def rank(e: str) -> tuple:
        label = _norm((visible.get(e) or {}).get("label"))
        return (label != nq, e not in indexed, not label.startswith(nq), len(label), e)
    ids = sorted(dict.fromkeys(indexed + contains), key=rank)
    rows = []
    for eid in ids[:limit]:
        a = visible.get(eid) or {}
        ent = get(eid) or {}
        rows.append({"entity": eid, "name": ent.get("display_name") or a.get("label") or eid,
                     "aliases": sorted(ent.get("aliases") or [])[:8], "industries": a.get("industries") or ent.get("industries") or [],
                     "cnpj_roots": list(ent.get("cnpj_roots") or [])[:5], "ticker": a.get("ticker") or ent.get("ticker"),
                     "ownership": a.get("ownership"), "certifications": [c.get("label") if isinstance(c, dict) else str(c)
                                            for c in (a.get("certifications") or [])][:5],
                     "url": "https://onssa.org/exec"})
    return _envelope("entities", rows, scoped.get("as_of"), SOURCE_REGISTRY,
                     note=None if rows else "Nenhuma entidade com esse nome/CNPJ nas indústrias da sua licença.")


def _entity_id(scoped: dict[str, Any], ref: str, resolve: Callable[[str], list[str]]) -> str | None:
    visible = scoped.get("entity_attrs") or {}
    if ref in visible:
        return ref
    for eid in resolve(ref):
        if eid in visible:
            return eid
    nr = _norm(ref)
    for eid, a in visible.items():
        if nr and nr == _norm(a.get("label")):
            return eid
    return None


def entity_signals(scoped: dict[str, Any], ref: str, days: int, limit: int, *,
                   resolve: Callable[[str], list[str]], today: dt.date | None = None) -> dict[str, Any]:
    eid = _entity_id(scoped, ref, resolve)
    if not eid:
        return {"isError": True, "message": "Entidade não encontrada nas indústrias da sua licença — use lookup_entity."}
    floor = ((today or dt.date.today()) - dt.timedelta(days=days)).isoformat()
    rows = []
    for c in scoped.get("feed") or []:
        if str(c.get("date") or "") < floor or (c.get("entity") != eid and eid not in (c.get("entities") or [])):
            continue
        ts = c.get("threat_score")
        rows.append({"date": c.get("date"), "kind": c.get("kind"), "lenses": c.get("lenses") or [],
                     "summary": str(c.get("narrative") or "")[:420],
                     "threat": round(float(ts) * 100) if isinstance(ts, (int, float)) and ts <= 1 else ts,
                     "alert": bool(c.get("is_alert")), "urls": _urls(c), "url": (_urls(c) or [None])[0]})
    enf = scoped.get("enforcement") or []
    for r in (enf.get("rows") or []) if isinstance(enf, dict) else enf:
        if (r.get("entity") == eid or eid in (r.get("entities") or [])) and str(r.get("date") or "") >= floor:
            urls = _urls(r) or ([r["url"]] if r.get("url") else [])
            rows.append({"date": r.get("date"), "kind": "enforcement:" + str(r.get("kind") or ""),
                         "lenses": ["sanctions"], "summary": str(r.get("title") or "")[:420],
                         "authority": r.get("authority"), "severity": r.get("severity"),
                         "url": (urls or [None])[0], "urls": urls})
    for d in scoped.get("distress") or []:
        if d.get("entity") == eid and str(d.get("date") or "") >= floor:
            rows.append({"date": d.get("date"), "kind": "distress", "lenses": ["distress"],
                         "summary": str(d.get("title") or d.get("label") or "")[:420],
                         "url": d.get("url"), "urls": [d.get("url")] if d.get("url") else []})
    rows.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    label = ((scoped.get("entity_attrs") or {}).get(eid) or {}).get("label") or eid
    return _envelope("signals", rows[:limit], scoped.get("as_of"), SOURCE_FEED, entity=eid, entity_name=label,
                     window_days=days)


def regulatory_events(scoped: dict[str, Any], industry: str | None, days: int,
                      today: dt.date | None = None) -> dict[str, Any]:
    floor = ((today or dt.date.today()) - dt.timedelta(days=days)).isoformat()
    ind = str(industry or "").strip().lower()
    rows = []
    for e in scoped.get("sector_events") or []:
        inds = [str(i).lower() for i in (e.get("industries") or [e.get("industry")]) if i]
        if str(e.get("date") or "") < floor or (ind and ind not in inds):
            continue
        srcs = [s for s in (e.get("sources") or []) if s.get("url")]
        official = [s for s in srcs if s.get("kind") == "official"]
        rows.append({"date": e.get("date"), "industries": inds, "title": e.get("title"),
                     "summary": str(e.get("summary") or "")[:420], "change": e.get("change_label"),
                     "severity": e.get("severity"), "affected_entities": e.get("n_affected"),
                     "url": (official or srcs or [{}])[0].get("url"),
                     "sources": [{"url": s["url"], "kind": s.get("kind"), "label": s.get("label")} for s in srcs[:6]]})
    rows.sort(key=lambda r: (str(r.get("date") or ""), r.get("severity") == "critical"), reverse=True)
    return _envelope("events", rows, scoped.get("as_of"), SOURCE_FEED + " Ato oficial (DOU) primeiro.",
                     window_days=days, industries=scoped.get("scoped_modules"))


# ---- JSON-RPC ------------------------------------------------------------------------------
def _text(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and payload.get("isError"):
        return {"isError": True, "content": [{"type": "text", "text": payload.get("message") or "erro"}]}
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=1)}],
            "structuredContent": payload}


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _ok(mid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


class Context:
    """Everything a request may touch, injected so each tool is unit-testable."""

    def __init__(self, *, principal: dict[str, Any], modules: list[str], elevated: bool,
                 feed: Callable[[], dict[str, Any]], resolve: Callable[[str], list[str]],
                 get_entity: Callable[[str], dict | None], ask: Callable[[str, dict], dict],
                 act: Callable[[dict], dict]) -> None:
        self.principal, self.modules, self.elevated = principal, modules, elevated
        self._feed, self.resolve, self.get_entity, self.ask, self.act = feed, resolve, get_entity, ask, act
        self._scoped: dict[str, Any] | None = None

    @property
    def scoped(self) -> dict[str, Any]:
        if self._scoped is None:
            self._scoped = scoped_feed(self.modules, self.elevated, self._feed)
        return self._scoped


def call_read_tool(name: str, args: dict[str, Any], ctx: Context) -> dict[str, Any]:
    args = args or {}
    if name == "lookup_entity":
        q = str(args.get("query") or "").strip()
        if not q:
            return _text({"isError": True, "message": "Informe `query` (nome, alias ou CNPJ)."})
        return _text(lookup_entity(ctx.scoped, q, _int(args.get("limit"), 5, 1, 20),
                                   resolve=ctx.resolve, get=ctx.get_entity))
    if name == "entity_signals":
        return _text(entity_signals(ctx.scoped, str(args.get("entity") or ""), _int(args.get("days"), 30, 1, 90),
                                    _int(args.get("limit"), 20, 1, 50), resolve=ctx.resolve))
    if name == "regulatory_events":
        return _text(regulatory_events(ctx.scoped, args.get("industry"), _int(args.get("days"), 30, 1, 120)))
    if name == "ask":
        q = str(args.get("q") or args.get("question") or "").strip()
        if not (3 <= len(q) <= 800):
            return _text({"isError": True, "message": "Faça uma pergunta de 3 a 800 caracteres."})
        scope = {"industry": str(args["industry"]).lower()} if args.get("industry") else {}
        return _text(ctx.ask(q, scope))
    return _text({"isError": True, "message": "Unknown tool: %s" % name})


def ops_tools(elevated: bool) -> list[dict[str, Any]]:
    """One tool per /api/act catalog intent — only for an elevated caller (#182)."""
    if not elevated:
        return []
    from src.dashboard import act_api

    out = []
    for intent, (cls, handler, _subj) in act_api._CATALOG.items():
        doc = " ".join(str(handler.__doc__ or "").split())
        read_only = intent.startswith("list_")
        out.append({
            "name": intent, "title": intent.replace("_", " "),
            "annotations": {"readOnlyHint": read_only, "destructiveHint": cls == "apply" and not read_only,
                            "idempotentHint": True, "openWorldHint": False},
            "description": ("[%s] %s Runs through Onça's typed /api/act catalog: authorized, "
                            "idempotent by `idempotency_key`, journaled to the curation log (ADR 020)."
                            % (cls, doc or intent)),
            "inputSchema": {"type": "object", "properties": {
                "args": {"type": "object", "description": "The intent's arguments (see the /api/act catalog)."},
                "idempotency_key": {"type": "string", "minLength": 8,
                                    "description": "Required. Reuse it to retry safely: a replay returns the stored result."},
                "officer": {"type": "string", "enum": ["cso", "cro", "cco", "cpo"]}},
                "required": ["idempotency_key"], "additionalProperties": False}})
    return out


def call_ops_tool(name: str, args: dict[str, Any], ctx: Context) -> dict[str, Any]:
    if not ctx.elevated:
        return _text({"isError": True, "message": "As ferramentas de operação exigem uma conta de operador Onça."})
    from src.dashboard import act_api

    if name not in act_api._CATALOG:
        return _text({"isError": True, "message": "Unknown tool: %s" % name})
    key = str((args or {}).get("idempotency_key") or "").strip()
    if len(key) < 8:
        return _text({"isError": True, "message": "`idempotency_key` (≥ 8 caracteres) é obrigatório."})
    body = {"intent": name, "args": (args or {}).get("args") or {}, "idempotency_key": key}
    if (args or {}).get("officer"):
        body["officer"] = args["officer"]
    return _text(ctx.act(body))


def handle_rpc(message: Any, *, path: str, ctx: Context) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _err(None, -32600, "Invalid Request")
    method, mid = message.get("method"), message.get("id")
    if mid is None:
        return None                                     # notification: no response at all
    if method == "initialize":
        ops = path == "/mcp/ops"
        return _ok(mid, {"protocolVersion": PROTOCOL_VERSION,
                         "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": dict(SERVER_INFO, name=SERVER_INFO["name"] + ("-ops" if ops else "")),
                         "instructions": (
                             "Operator write tools over Onça's /api/act catalog. Every call needs an "
                             "idempotency_key and is journaled." if ops else
                             "Brazilian financial-sector competitive and regulatory intelligence for YOUR "
                             "licensed industries: curated entity registry, dated signals with primary-source "
                             "links, sector-wide regulatory events, and a grounded `ask`. The first three tools "
                             "are deterministic; `ask` is a model and can be wrong. Always cite the `url`.")})
    if method == "tools/list":
        return _ok(mid, {"tools": ops_tools(ctx.elevated) if path == "/mcp/ops" else read_tools()})
    if method == "tools/call":
        p = message.get("params") or {}
        fn = call_ops_tool if path == "/mcp/ops" else call_read_tool
        try:
            return _ok(mid, fn(str(p.get("name") or ""), p.get("arguments") or {}, ctx))
        except Exception as exc:  # noqa: BLE001 - a tool result the agent can read, never a 500
            print("mcp: tool_error " + json.dumps({"tool": p.get("name"), "type": type(exc).__name__}))
            return _ok(mid, _text({"isError": True, "message": "Falha interna nesta ferramenta — tente de novo."}))
    if method == "ping":
        return _ok(mid, {})
    return _err(mid, -32601, "Method not found: %s" % method)


def handle_a2a(message: Any, ctx: Context) -> dict[str, Any] | None:
    """A2A JSON-RPC (#186): ``message/send`` → grounded ask → an agent Message."""
    if not isinstance(message, dict):
        return _err(None, -32600, "Invalid Request")
    method, mid = message.get("method"), message.get("id")
    if method != "message/send":
        return _err(mid, -32601, "Method not found: %s (this agent supports message/send)" % method)
    msg = (message.get("params") or {}).get("message") or {}
    text = " ".join(str(p.get("text") or "") for p in msg.get("parts") or []
                    if isinstance(p, dict) and p.get("kind", "text") == "text").strip()
    if not (3 <= len(text) <= 800):
        return _err(mid, -32602, "Send one text part of 3–800 characters.")
    res = ctx.ask(text, {})
    parts = [{"kind": "text", "text": str(res.get("answer") or "Sem resposta fundamentada.")}]
    if res.get("citations"):
        parts.append({"kind": "data", "data": {"citations": res["citations"], "grounded": res.get("grounded")}})
    return _ok(mid, {"kind": "message", "role": "agent", "messageId": "onca-" + str(int(time.time() * 1000)),
                     "contextId": msg.get("contextId"), "parts": parts})


# ---- Lambda wiring ---------------------------------------------------------------------------
def _lambda(name: str) -> Any:
    if "lambda" not in _cache:
        import boto3
        _cache["lambda"] = boto3.client("lambda")
    return _cache["lambda"]


def _claims_event(principal: dict[str, Any], path: str, body: dict[str, Any]) -> dict[str, Any]:
    """The event shape API Gateway's JWT authorizer produces — so /api/ask and /api/act apply
    their own identity rules (Phase D read boundary; elevated-only writes) unchanged."""
    return {"rawPath": path, "headers": {"content-type": "application/json"}, "isBase64Encoded": False,
            "body": json.dumps(body, ensure_ascii=False),
            "requestContext": {"http": {"method": "POST"}, "authorizer": {"jwt": {"claims": {
                "sub": principal["sub"], "custom:tenant": principal.get("tenant") or "",
                "cognito:groups": list(principal.get("groups") or [])}}}}}


def _invoke(fn_env: str, event: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    out = _lambda("x").invoke(FunctionName=os.environ[fn_env], Payload=json.dumps(event).encode())
    resp = json.loads(out["Payload"].read() or b"{}")
    try:
        body = json.loads(resp.get("body") or "{}")
    except (TypeError, ValueError):
        body = {}
    return int(resp.get("statusCode") or 500), body


def _public_keys() -> dict[str, bytes]:
    if "keys" not in _cache:
        import boto3
        from src.dashboard.oauth import tokens
        der = boto3.client("kms").get_public_key(KeyId=os.environ["ONCA_OAUTH_KMS_KEY"])["PublicKey"]
        _cache["keys"] = {tokens.kid_for(der): der}
    return _cache["keys"]


def _json_resp(status: int, body: Any, headers: dict[str, str] | None = None) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "no-store",
                                              **(headers or {})},
            "body": json.dumps(body, ensure_ascii=False) if body is not None else ""}


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    from src.dashboard.oauth import config, resource
    from src.dashboard.oauth.handler import killed
    from src.dashboard.oauth.store import DynamoStore

    path = (event.get("rawPath") or "/mcp").rstrip("/") or "/mcp"
    if path not in config.RESOURCES:
        return _json_resp(404, {"error": "not found"})
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "POST").upper()
    if method != "POST":
        return _json_resp(405, {"error": "POST JSON-RPC to this endpoint"}, {"allow": "POST"})
    s = config.settings()
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError):
        return _json_resp(400, _err(None, -32700, "Parse error"))
    if killed():
        p, why = None, "disabled"
    else:
        store = DynamoStore(os.environ["ONCA_OAUTH_TABLE"])
        p, why = resource.principal(headers, path=path, settings=s, public_keys=_public_keys(),
                                    family_lookup=store.get)
    if p is None:
        print("mcp: " + json.dumps({"path": path, "status": 401, "reason": why,
                                    **request_log_fields(parsed, headers, None)}, ensure_ascii=False))
        return _json_resp(401, _err(None, -32001, "Sign in to Onça to use this server (OAuth)."),
                          {"www-authenticate": resource.challenge(s, path, None if why == "missing" else "invalid_token")})
    from src.dashboard.push import modules_for
    from src.dashboard.oauth.handler import ELEVATED_GROUPS
    from src.synth import entity_registry

    elevated = bool(ELEVATED_GROUPS.intersection(p.get("groups") or []))
    modules = modules_for(p.get("tenant"), p.get("groups") or [])
    if not modules and not elevated:
        return _json_resp(403, _err(None, -32003, "Esta conta não tem uma licença Onça ativa."))

    def resolve(q: str) -> list[str]:
        if q.startswith("cnpj:"):
            hit = entity_registry.resolve_by_cnpj(q[5:])
            return [hit] if hit else []
        return entity_registry.resolve_by_name(q)

    def ask(q: str, scope: dict) -> dict:
        status, body = _invoke("ONCA_AGENT_FN", _claims_event(p, "/api/ask", {"q": q, "scope": scope}))
        if status != 200:
            return {"isError": True, "message": body.get("error") or "A consulta falhou (%d)." % status}
        return {"answer": body.get("answer"), "grounded": body.get("grounded"), "refused": body.get("refused"),
                "citations": [{"id": c.get("id"), "label": c.get("entity_label"), "date": c.get("date"),
                               "url": (c.get("sources") or [{}])[0].get("url") if c.get("sources") else c.get("url")}
                              for c in body.get("citations") or []],
                "source": SOURCE_FEED, "license": LICENSE,
                "note": "Resposta de modelo de linguagem fundamentada nos dados licenciados — confira as citações."}

    def act(body: dict) -> dict:
        status, out = _invoke("ONCA_ACT_FN", _claims_event(p, "/api/act", body))
        if status >= 400:
            return {"isError": True, "message": out.get("error") or "act %d" % status}
        return out

    ctx = Context(principal=p, modules=modules, elevated=elevated, feed=load_feed, resolve=resolve,
                  get_entity=entity_registry.get_entity, ask=ask, act=act)
    handler = (lambda m: handle_a2a(m, ctx)) if path == "/a2a" else (lambda m: handle_rpc(m, path=path, ctx=ctx))
    if isinstance(parsed, list):
        out = [r for m in parsed if (r := handler(m)) is not None]
        resp = _json_resp(202, None) if not out else _json_resp(200, out)
    else:
        r = handler(parsed)
        resp = _json_resp(202, None) if r is None else _json_resp(200, r)
    print("mcp: " + json.dumps({"path": path, "status": resp["statusCode"],
                                "method": parsed.get("method") if isinstance(parsed, dict) else "batch",
                                "tool": ((parsed.get("params") or {}).get("name") if isinstance(parsed, dict) else None),
                                **request_log_fields(parsed, headers, p)}, ensure_ascii=False))
    return resp


def _short_hash(v: Any) -> str | None:
    return hashlib.sha256(str(v).encode()).hexdigest()[:12] if v else None


def request_log_fields(parsed: Any, headers: dict[str, Any], principal: dict[str, Any] | None) -> dict[str, Any]:
    """Who is calling and how — 2026-10-04 claude.ai looped initialize→tools/list ~150× in 23 min
    and the log line couldn't tell one client/conversation from another. The user is a hash
    (privacy policy: logs carry no direct identifiers); client_id is a public CIMD URL."""
    msg = parsed if isinstance(parsed, dict) else {}
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    out: dict[str, Any] = {
        "rpc_id": msg.get("id") if isinstance(msg.get("id"), (str, int)) else None,
        "proto_hdr": headers.get("mcp-protocol-version"),
        "ua": str(headers.get("user-agent") or "")[:120] or None,
        "client_id": (principal or {}).get("client_id"),
        "user": _short_hash((principal or {}).get("sub")),
        "session": headers.get("mcp-session-id"),
    }
    if msg.get("method") == "initialize":
        ci = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        out["proto_req"] = params.get("protocolVersion")
        out["client"] = "%s/%s" % (str(ci.get("name") or "?")[:60], str(ci.get("version") or "?")[:30])
    if isinstance(parsed, list):
        out["batch"] = len(parsed)
    return {k: v for k, v in out.items() if v is not None}
