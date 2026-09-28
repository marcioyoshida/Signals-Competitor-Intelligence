"""The authorization server's protocol logic — ported from Tarantula #99. Pure: every AWS call
is injected. Onça differences: the principal is a PROVISIONED user (``principal_of(sub)`` → tenant,
groups, licensed modules, elevated — ``None`` refuses sign-in: tenants are provisioned, never
self-created here); three audience-bound resources (/mcp, /mcp/ops, /a2a) with one scope each;
``onca:write`` (/mcp/ops) only for elevated (operator/admin group) users, re-checked on refresh.

Requests are ``{"method", "path", "query", "form", "headers", "cookies"}`` (headers
lower-cased); responses are ``{"status", "headers", "cookies", "body"}``. Threat IDs
(T#) refer to docs/2026-09-27-threat-model-mcp-oauth.md.
"""
from __future__ import annotations

import hmac
import json
import time
from dataclasses import dataclass
from html import escape
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

from src.dashboard.oauth import cimd, config, tokens

CSRF_COOKIE = "__Host-oauth_csrf"

# T5 clickjacking; no script at all on these pages. form-action is deliberately NOT
# set: browsers apply it to the redirect after the consent POST, which would block the
# legitimate redirect to the client.
_HTML_HEADERS = {
    "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
    # same-origin, NOT no-referrer: under no-referrer browsers send `Origin: null` on the
    # consent form POST, which the T4 origin check (rightly) rejects — found live. The
    # callback URL (Cognito code+state in its query) still never leaks cross-site.
    "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                               "frame-ancestors 'none'; base-uri 'none'",
}
_JSON_HEADERS = {"Content-Type": "application/json", "Cache-Control": "no-store",
                 "Pragma": "no-cache"}


@dataclass
class Cognito:
    """The one static upstream client (D2). ``exchange`` returns the ID-token claims."""
    authorize_url: Callable[[str, str, str], str]      # (state, nonce, code_challenge)
    exchange: Callable[[str, str], dict[str, Any]]     # (code, code_verifier) -> claims


def _resp(status: int, body: str = "", headers: dict | None = None,
          cookies: list[str] | None = None) -> dict[str, Any]:
    return {"status": status, "headers": headers or {}, "cookies": cookies or [], "body": body}


def _json(status: int, obj: Any, extra: dict | None = None) -> dict[str, Any]:
    return _resp(status, json.dumps(obj), dict(_JSON_HEADERS, **(extra or {})))


def _page(status: int, title: str, inner: str, cookies: list[str] | None = None) -> dict:
    html = ("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>%s</title><style>body{font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;"
            "max-width:30rem;margin:3rem auto;padding:0 1rem;color:#141c2b}"
            ".box{border:1px solid #dde4f0;border-radius:14px;padding:1.2rem 1.4rem}"
            ".host{font-family:ui-monospace,Menlo,monospace;background:#f5f7fc;padding:.1rem .35rem;"
            "border-radius:5px}.warn{background:#fff4e5;border:1px solid #f0c27a;border-radius:10px;"
            "padding:.6rem .8rem;margin:.8rem 0}.ok{color:#0f7a55;font-weight:600}"
            "button{font:600 15px/1 sans-serif;padding:.7rem 1.2rem;border-radius:999px;border:0;"
            "cursor:pointer;margin:.3rem .4rem 0 0}.allow{background:#2f6fd6;color:#fff}"
            ".deny{background:#e6eaf2;color:#141c2b}</style></head><body>%s</body></html>"
            % (escape(title), inner))
    return _resp(status, html, dict(_HTML_HEADERS), cookies)


def error_page(message: str, status: int = 400) -> dict[str, Any]:
    """An error we must NOT redirect (unknown client or redirect_uri) — T2."""
    return _page(status, "Erro de acesso",
                 "<div class=box><h1>Não foi possível continuar</h1><p>%s</p></div>" % escape(message))


def with_params(uri: str, params: dict[str, str]) -> str:
    return uri + ("&" if urlsplit(uri).query else "?") + urlencode(
        {k: v for k, v in params.items() if v is not None})


class AuthServer:
    def __init__(self, *, settings: config.Settings, store: Any,
                 sign_der: Callable[[bytes], bytes], public_key_der: bytes,
                 cognito: Cognito, principal_of: Callable[[str], dict[str, Any] | None],
                 fetch_cimd: Callable[[str], dict[str, Any]] = cimd.fetch,
                 now: Callable[[], float] = time.time) -> None:
        self.s, self.store, self.sign_der = settings, store, sign_der
        self.public_key_der, self.kid = public_key_der, tokens.kid_for(public_key_der)
        self.cognito, self.principal_of, self.fetch_cimd, self.now = cognito, principal_of, fetch_cimd, now

    # ---- discovery --------------------------------------------------------------------
    def protected_resource_metadata(self, path: str = "/mcp") -> dict:
        if path not in config.RESOURCES:
            return _json(404, {"error": "unknown resource"})
        return _json(200, {"resource": self.s.resource_uri(path),
                           "authorization_servers": [self.s.issuer],
                           "scopes_supported": [config.RESOURCES[path]],
                           "bearer_methods_supported": ["header"],
                           "resource_name": {"/mcp": "Onça (MCP)", "/mcp/ops": "Onça ops (MCP, operators)",
                                             "/a2a": "Onça (A2A)"}[path],
                           "resource_documentation": self.s.base + "/llms.txt"},
                     {"Cache-Control": "public, max-age=3600"})

    def authorization_server_metadata(self) -> dict:
        i = self.s.issuer
        return _json(200, {
            "issuer": i, "authorization_endpoint": i + "/authorize",
            "token_endpoint": i + "/token", "revocation_endpoint": i + "/revoke",
            "jwks_uri": i + "/jwks.json", "scopes_supported": [config.READ, config.WRITE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],          # T1: S256 only
            # "none" for public (CIMD) clients; a secret only for PRE-REGISTERED confidential
            # clients (e.g. a Copilot Studio connector, #184 — its "Manual" OAuth mode)
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True,         # D3: CIMD, no DCR
            "authorization_response_iss_parameter_supported": True,  # T6: RFC 9207
            "service_documentation": self.s.base + "/llms.txt",
            "op_policy_uri": self.s.base + "/docs/privacy.html",   # RFC 8414 §2
            "op_tos_uri": self.s.base + "/docs/terms.html"},
            {"Cache-Control": "public, max-age=3600"})

    def jwks(self) -> dict:
        return _json(200, {"keys": [tokens.jwk(self.public_key_der)]},
                     {"Cache-Control": "public, max-age=300"})

    # ---- client resolution (D3) -------------------------------------------------------
    def resolve_client(self, client_id: str) -> dict[str, Any]:
        if cimd.is_cimd_client_id(client_id):
            key = "cimd#" + tokens.sha256_hex(client_id)
            cached = self.store.get(key)
            if cached:
                return cached
            doc = self.fetch_cimd(client_id)              # raises CIMDError (T8)
            self.store.put(key, "-", doc, ttl=config.CIMD_CACHE_MAX_S)
            return doc
        reg = self.store.get("client#" + client_id)
        if not reg:
            raise cimd.CIMDError("unknown client_id")
        return reg

    # ---- /authorize --------------------------------------------------------------------
    def authorize(self, req: dict) -> dict:
        q = req.get("query") or {}
        client_id, redirect_uri = q.get("client_id") or "", q.get("redirect_uri") or ""
        if not client_id or not redirect_uri:
            return error_page("client_id and redirect_uri are required.")
        try:
            client = self.resolve_client(client_id)
        except cimd.CIMDError as exc:
            return error_page(str(exc))
        # T2: never redirect to a URI the client didn't register.
        if not cimd.redirect_allowed(redirect_uri, client.get("redirect_uris") or []):
            return error_page("redirect_uri is not registered for this client.")
        state = q.get("state")

        def fail(code: str, desc: str) -> dict:
            return _resp(302, headers={"Location": with_params(redirect_uri, {
                "error": code, "error_description": desc, "state": state,
                "iss": self.s.issuer})})                   # T6: iss on errors too

        if q.get("response_type") != "code":
            return fail("unsupported_response_type", "response_type must be code")
        # T1: PKCE S256 is required — except for a pre-registered CONFIDENTIAL client, which
        # proves itself with its secret at /token instead (connectors that don't send PKCE)
        if q.get("code_challenge") or not client.get("confidential"):
            if not q.get("code_challenge") or q.get("code_challenge_method") != "S256":
                return fail("invalid_request", "PKCE with S256 is required")
        # no `resource` param (RFC 8707 is optional for some platform clients, e.g. Gemini
        # Enterprise): a pre-registered client's own default resource, else /mcp
        rpath = (self.s.resource_path(q.get("resource")) if q.get("resource")
                 else client.get("default_resource") or "/mcp")
        if rpath is None:
            return fail("invalid_target", "unknown resource")
        allowed = config.RESOURCES[rpath]
        scope = q.get("scope") or allowed
        if set(scope.split()) - {allowed}:
            return fail("invalid_scope", "supported scope for this resource: " + allowed)

        req_id, cstate = tokens.random_token(16), tokens.random_token(24)
        cverifier, nonce = tokens.random_token(48), tokens.random_token(16)
        self.store.put("req#" + req_id, "-", {
            "client_id": client_id, "client_name": client["client_name"],
            "redirect_uri": redirect_uri, "code_challenge": q.get("code_challenge") or "",
            "state": state, "scope": allowed, "resource": self.s.resource_uri(rpath),
            "doc_hash": client.get("doc_hash", ""), "cstate": cstate,
            "cverifier": cverifier, "nonce": nonce,
            "loopback_only": cimd.loopback_only(client.get("redirect_uris") or [])},
            ttl=config.REQUEST_TTL_S)
        # T3: the Cognito leg has its OWN state + nonce + PKCE, bound to this request.
        return _resp(302, headers={"Location": self.cognito.authorize_url(
            req_id + "." + cstate, nonce, tokens.pkce_s256(cverifier))})

    # ---- /callback (from Cognito) -----------------------------------------------------
    def callback(self, req: dict) -> dict:
        q = req.get("query") or {}
        req_id, _, cstate = str(q.get("state") or "").partition(".")
        pending = self.store.get("req#" + req_id) if req_id else None
        if (not pending or pending.get("org")
                or not hmac.compare_digest(str(pending.get("cstate", "")), cstate)):
            return error_page("Este link de acesso expirou ou já foi usado. "
                              "Recomece a partir do seu app.")
        if not q.get("code"):
            return self._redirect_error(pending, "access_denied", "sign-in was cancelled")
        try:
            claims = self.cognito.exchange(q["code"], pending["cverifier"])
        except Exception:  # noqa: BLE001 - upstream failure, never detail it
            return error_page("O login falhou. Tente de novo.", 502)
        if claims.get("nonce") != pending["nonce"] or not claims.get("sub"):
            return error_page("Não foi possível verificar o login. Tente de novo.")
        who = self.principal_of(claims["sub"])
        if not who or not (who.get("modules") or who.get("elevated")):
            # tenants are provisioned, never created by an OAuth sign-in (#179 decision)
            self.store.delete("req#" + req_id)
            return error_page("Esta conta não tem uma licença Onça ativa. Fale com "
                              "contato@onssa.org para provisionar o seu acesso.", 403)
        if pending["scope"] == config.WRITE and not who.get("elevated"):
            self.store.delete("req#" + req_id)
            return self._redirect_error(pending, "access_denied",
                                        "the ops tools require an Onça operator account")
        sub = claims["sub"]
        fields = {"org": sub, "tenant": who.get("tenant"), "groups": list(who.get("groups") or [])}
        self.store.update("req#" + req_id, "-", fields)
        pending.update(fields)
        org = sub
        if self._has_consent(org, pending):
            return self._issue_code(req_id, pending)
        return self._consent_page(req_id, pending)

    def _consent_key(self, org: str) -> str:
        return "usr#" + org

    def _has_consent(self, org: str, pending: dict) -> bool:
        c = self.store.get(self._consent_key(org), "consent#" + pending["client_id"])
        return bool(c and c.get("scope") == pending["scope"]
                    and c.get("redirect_host") == urlsplit(pending["redirect_uri"]).hostname
                    and c.get("doc_hash") == pending.get("doc_hash", ""))   # T3 re-prompt

    def _consent_page(self, req_id: str, pending: dict) -> dict:
        csrf = tokens.random_token(24)
        self.store.update("req#" + req_id, "-", {"csrf": csrf})
        cid = pending["client_id"]
        host = urlsplit(cid).hostname if cimd.is_cimd_client_id(cid) else None
        verified = bool(host and host in self.s.verified_hosts)
        redirect_host = urlsplit(pending["redirect_uri"]).hostname or ""
        who = ("<span class=host>%s</span>%s" % (escape(host), " <span class=ok>✓ verificado</span>"
                                                  if verified else "")
               if host else "um cliente registrado")
        warn = ("<div class=warn>Este app recebe o seu login <b>neste computador</b> "
                "(<span class=host>%s</span>). Só continue se você acabou de iniciar isto a partir "
                "de um app em que confia.</div>" % escape(redirect_host)) if pending.get("loopback_only") else ""
        what = ("<b>executar ações do catálogo de operação</b> (curadoria, execuções do pipeline) em seu nome"
                if pending["scope"] == config.WRITE else
                "<b>ler os dados da sua licença Onça</b> (entidades, sinais, eventos regulatórios) e "
                "<b>perguntar à Onça</b>. Não pode alterar nada")
        inner = (
            "<div class=box><h1>Permitir acesso à Onça?</h1>"
            "<p><b>%s</b> (de %s) quer usar a sua conta Onça.</p>%s"
            "<p>Poderá: %s.</p>"
            "<p>Depois de permitir, você será enviado para <span class=host>%s</span>.</p>"
            "<form method=post action=\"%s/consent\">"
            "<input type=hidden name=req value=\"%s\"><input type=hidden name=csrf value=\"%s\">"
            "<button class=allow name=decision value=allow>Permitir</button>"
            "<button class=deny name=decision value=deny>Negar</button></form>"
            "<p style=\"font-size:13px;color:#5d6c86\">Você pode desconectar o app a qualquer momento "
            "(contato@onssa.org). <a href=\"%s/docs/privacy.html\">Privacidade</a> · "
            "<a href=\"%s/docs/terms.html\">Termos</a></p></div>"
            % (escape(pending["client_name"]), who, warn, what, escape(redirect_host),
               escape(urlsplit(self.s.issuer).path), escape(req_id), escape(csrf),
               escape(self.s.base), escape(self.s.base)))
        cookie = ("%s=%s; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=%d"
                  % (CSRF_COOKIE, csrf, config.REQUEST_TTL_S))
        return _page(200, "Permitir acesso à Onça?", inner, [cookie])

    # ---- /consent (POST) ---------------------------------------------------------------
    def consent(self, req: dict) -> dict:
        if req.get("method") != "POST":
            return _resp(405, headers={"Allow": "POST"})
        h, f = req.get("headers") or {}, req.get("form") or {}
        # T4: same-origin only, and the CSRF token must match form, cookie AND request.
        def reject(reason: str, msg: str) -> dict:
            print("oauth: consent_rejected " + reason)      # an enum, never a value (T16)
            return error_page(msg, 403)

        if h.get("sec-fetch-site") not in (None, "", "same-origin", "none"):
            return reject("sec_fetch_site", "Solicitação bloqueada.")
        origin = h.get("origin")
        if origin and origin.rstrip("/") != self.s.base:
            return reject("origin", "Solicitação bloqueada.")
        req_id, csrf = f.get("req") or "", f.get("csrf") or ""
        pending = self.store.get("req#" + req_id) if req_id else None
        cookie = (req.get("cookies") or {}).get(CSRF_COOKIE, "")
        for reason, bad in (("no_request", not pending),
                            ("not_signed_in", pending and not pending.get("org")),
                            ("no_form_csrf", not csrf),
                            ("form_csrf_mismatch", pending and pending.get("csrf") and csrf
                             and not hmac.compare_digest(csrf, pending["csrf"])),
                            ("no_cookie", not cookie),
                            ("cookie_mismatch", cookie and csrf
                             and not hmac.compare_digest(csrf, cookie))):
            if bad:
                return reject(reason, "Esta solicitação expirou. Recomece a partir do seu app.")
        if f.get("decision") != "allow":
            self.store.delete("req#" + req_id)
            return self._redirect_error(pending, "access_denied", "the user denied access")
        self.store.put(self._consent_key(pending["org"]), "consent#" + pending["client_id"], {
            "client_id": pending["client_id"], "client_name": pending["client_name"],
            "scope": pending["scope"], "doc_hash": pending.get("doc_hash", ""),
            "redirect_host": urlsplit(pending["redirect_uri"]).hostname,
            "created": int(self.now())})
        return self._issue_code(req_id, pending)

    def _redirect_error(self, pending: dict, code: str, desc: str) -> dict:
        return _resp(302, headers={"Location": with_params(pending["redirect_uri"], {
            "error": code, "error_description": desc, "state": pending.get("state"),
            "iss": self.s.issuer})})

    def _issue_code(self, req_id: str, pending: dict) -> dict:
        code = tokens.random_token(32)
        self.store.put("code#" + tokens.sha256_hex(code), "-", {
            k: pending.get(k) for k in ("client_id", "redirect_uri", "code_challenge", "resource",
                                        "scope", "org", "tenant", "groups", "client_name")} | {
            "created": int(self.now())}, ttl=config.CODE_TTL_S)
        self.store.delete("req#" + req_id)               # one authorization, one code
        return _resp(302, headers={"Location": with_params(pending["redirect_uri"], {
            "code": code, "state": pending.get("state"), "iss": self.s.issuer})},
            cookies=["%s=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0" % CSRF_COOKIE])

    # ---- /token ------------------------------------------------------------------------
    def token(self, req: dict) -> dict:
        if req.get("method") != "POST":
            return _resp(405, headers={"Allow": "POST"})
        f = req.get("form") or {}
        if f.get("resource") and self.s.resource_path(f["resource"]) is None:
            return self._terr("invalid_target", "unknown resource")
        grant = f.get("grant_type")
        if grant == "authorization_code":
            return self._code_grant(f, req.get("headers"))
        if grant == "refresh_token":
            if not self.client_auth_ok(f, req.get("headers")):
                return self._terr("invalid_client", "client authentication failed", 401)
            return self._refresh_grant(f)
        return self._terr("unsupported_grant_type", "authorization_code or refresh_token")

    @staticmethod
    def _terr(code: str, desc: str, status: int = 400) -> dict:
        return _json(status, {"error": code, "error_description": desc})

    def client_auth_ok(self, f: dict, headers: dict | None) -> bool:
        """A pre-registered CONFIDENTIAL client must present its secret (post or HTTP Basic),
        checked against the stored SHA-256; a public client needs none. Constant-time."""
        cid = f.get("client_id") or ""
        secret = f.get("client_secret") or ""
        auth = (headers or {}).get("authorization") or ""
        if auth.lower().startswith("basic "):
            import base64 as _b64
            from urllib.parse import unquote
            try:
                bid, _, bsec = _b64.b64decode(auth[6:].strip()).decode().partition(":")
                cid, secret = cid or unquote(bid), unquote(bsec)
                f["client_id"] = cid
            except Exception:  # noqa: BLE001
                return False
        reg = self.store.get("client#" + cid) if cid and not cimd.is_cimd_client_id(cid) else None
        if not reg or not reg.get("confidential"):
            return not secret                      # public client: nothing to check
        return bool(secret) and hmac.compare_digest(tokens.sha256_hex(secret), str(reg.get("secret_sha256") or ""))

    def _code_grant(self, f: dict, headers: dict | None = None) -> dict:
        code, verifier = f.get("code") or "", f.get("code_verifier") or ""
        if not self.client_auth_ok(f, headers):
            return self._terr("invalid_client", "client authentication failed", 401)
        if not code or not f.get("client_id") or not f.get("redirect_uri"):
            return self._terr("invalid_request", "code, client_id and redirect_uri are required")
        item = self.store.take("code#" + tokens.sha256_hex(code))     # T1: single use
        now = int(self.now())
        challenge = str((item or {}).get("code_challenge") or "")
        if challenge:
            pkce_ok = bool(verifier) and hmac.compare_digest(tokens.pkce_s256(verifier), challenge)
        else:   # PKCE-less only ever issued to a confidential client, which just authenticated
            reg = self.store.get("client#" + f["client_id"]) or {}
            pkce_ok = bool(reg.get("confidential"))
        if (not item or now - int(item["created"]) > config.CODE_TTL_S
                or item["client_id"] != f["client_id"]
                or item["redirect_uri"] != f["redirect_uri"]
                or (f.get("resource") and f["resource"].rstrip("/") != item["resource"])
                or not pkce_ok):
            return self._terr("invalid_grant", "invalid, expired or already used code")
        fid = tokens.random_token(16)
        self.store.put("usr#" + item["org"], "fam#" + fid, {
            "client_id": item["client_id"], "client_name": item.get("client_name", ""),
            "resource": item["resource"], "created": now, "last_used": now, "revoked": False},
            ttl=config.REFRESH_ABSOLUTE_S)
        return self._grant(sub=item["org"], client_id=item["client_id"], scope=item["scope"],
                           resource=item["resource"], tenant=item.get("tenant"),
                           groups=item.get("groups") or [], fid=fid, now=now)

    def _grant(self, *, sub, client_id, scope, resource, tenant, groups, fid, now) -> dict:
        rt = tokens.random_token(32)
        self.store.put("rt#" + tokens.sha256_hex(rt), "-", {
            "org": sub, "client_id": client_id, "fid": fid, "scope": scope, "resource": resource,
            "created": now, "used": False}, ttl=config.REFRESH_ABSOLUTE_S)
        access = tokens.mint(tokens.access_claims(
            settings=self.s, resource_path=self.s.resource_path(resource) or "/mcp", sub=sub,
            tenant=tenant, groups=groups, client_id=client_id, scope=scope,
            family_id=fid, now=now), kid=self.kid, sign_der=self.sign_der)
        return _json(200, {"access_token": access, "token_type": "Bearer",
                           "expires_in": config.ACCESS_TTL_S, "refresh_token": rt,
                           "scope": scope})

    def _refresh_grant(self, f: dict) -> dict:
        raw = f.get("refresh_token") or ""
        if not raw or not f.get("client_id"):
            return self._terr("invalid_request", "refresh_token and client_id are required")
        key = "rt#" + tokens.sha256_hex(raw)
        rt = self.store.get(key)
        if not rt or rt["client_id"] != f["client_id"]:
            return self._terr("invalid_grant", "invalid refresh token")
        fam = self.store.get("usr#" + rt["org"], "fam#" + rt["fid"])
        if not fam or fam.get("revoked"):
            return self._terr("invalid_grant", "refresh token revoked")
        if not self.store.mark_used(key):
            # T12: a rotated token came back — assume theft, kill the whole family.
            self.revoke_family(rt["org"], rt["fid"])
            print("oauth: refresh_reuse_detected")
            return self._terr("invalid_grant", "refresh token reused; access revoked")
        now = int(self.now())
        if (now - int(fam["created"]) > config.REFRESH_ABSOLUTE_S
                or now - int(rt["created"]) > config.REFRESH_IDLE_S):
            return self._terr("invalid_grant", "refresh token expired")
        # T13: entitlement is re-read from Cognito + the tenant record NOW — a de-provisioned
        # user (or an operator who lost the group) gets no new access token.
        who = self.principal_of(rt["org"])
        if (not who or not (who.get("modules") or who.get("elevated"))
                or (rt["scope"] == config.WRITE and not who.get("elevated"))):
            self.revoke_family(rt["org"], rt["fid"])
            return self._terr("invalid_grant", "account no longer entitled")
        self.store.update("usr#" + rt["org"], "fam#" + rt["fid"], {"last_used": now})
        return self._grant(sub=rt["org"], client_id=rt["client_id"], scope=rt["scope"],
                           resource=rt.get("resource") or self.s.resource_uri("/mcp"),
                           tenant=who.get("tenant"), groups=who.get("groups") or [],
                           fid=rt["fid"], now=now)

    # ---- revocation (RFC 7009 + Connected apps) ---------------------------------------
    def revoke_family(self, org: str, fid: str) -> None:
        self.store.update("usr#" + org, "fam#" + fid, {"revoked": True})

    def revoke(self, req: dict) -> dict:
        """RFC 7009: always 200, whether or not the token was known."""
        raw = (req.get("form") or {}).get("token") or ""
        rt = self.store.get("rt#" + tokens.sha256_hex(raw)) if raw else None
        if rt:
            self.revoke_family(rt["org"], rt["fid"])
        return _resp(200, headers={"Cache-Control": "no-store"})
