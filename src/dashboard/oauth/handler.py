"""Lambda entry for Onça's MCP authorization server: wires AuthServer to AWS.

Reached through the Cognito HTTP API (routes WITHOUT the JWT authorizer — this IS the
authorization server) behind exact-path CloudFront behaviors with no edge gate, so an MCP
client can always read the metadata and start a sign-in (the Tarantula "unreachable handshake"
trap).

* Kill switch: SSM ``ONCA_OAUTH_KILL_PARAM`` = ``true`` refuses every flow endpoint (and makes
  the MCP server reject every bearer token); metadata keeps answering.
* T16: one log line per request — route and status, never a parameter value.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode

from src.dashboard.oauth import config, server
from src.dashboard.oauth.store import DynamoStore

_cache: dict[str, Any] = {}
_KILL_TTL_S = 60
ELEVATED_GROUPS = {"operator", "admin"}


def killed() -> bool:
    name = os.environ.get("ONCA_OAUTH_KILL_PARAM")
    if not name:
        return False
    hit = _cache.get("kill")
    if hit and time.time() - hit[0] < _KILL_TTL_S:
        return hit[1]
    try:
        import boto3
        val = boto3.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]
        k = str(val).strip().lower() == "true"
    except Exception as exc:  # noqa: BLE001 - unreadable switch: stay up, but say so
        print("oauth: kill switch unreadable (%s)" % type(exc).__name__)
        k = False
    _cache["kill"] = (time.time(), k)
    return k


def _kms():
    if "kms" not in _cache:
        import boto3
        _cache["kms"] = boto3.client("kms")
    return _cache["kms"]


def public_key_der() -> bytes:
    if "pub" not in _cache:
        _cache["pub"] = _kms().get_public_key(KeyId=os.environ["ONCA_OAUTH_KMS_KEY"])["PublicKey"]
    return _cache["pub"]


def _sign_der(message: bytes) -> bytes:
    return _kms().sign(KeyId=os.environ["ONCA_OAUTH_KMS_KEY"], Message=message,
                       MessageType="RAW", SigningAlgorithm="ECDSA_SHA_256")["Signature"]


def principal_of(sub: str) -> dict[str, Any] | None:
    """Live entitlement for a Cognito user: tenant, groups, licensed modules, elevated.
    ``None`` when the user doesn't exist (or can't be read — fail closed)."""
    import boto3

    pool = os.environ["ONCA_USER_POOL_ID"]
    cog = boto3.client("cognito-idp")
    try:
        users = cog.list_users(UserPoolId=pool, Filter='sub = "%s"' % sub.replace('"', ""),
                               Limit=1).get("Users") or []
        if not users or not users[0].get("Enabled", True):
            return None
        u = users[0]
        attrs = {a["Name"]: a["Value"] for a in u.get("Attributes") or []}
        groups = [g["GroupName"] for g in cog.admin_list_groups_for_user(
            UserPoolId=pool, Username=u["Username"]).get("Groups") or []]
    except Exception as exc:  # noqa: BLE001
        print("oauth: principal lookup failed (%s)" % type(exc).__name__)
        return None
    tenant = attrs.get("custom:tenant") or None
    from src.dashboard.push import modules_for
    return {"sub": sub, "tenant": tenant, "groups": groups,
            "modules": modules_for(tenant, groups),
            "elevated": bool(ELEVATED_GROUPS.intersection(groups))}


def _cognito(settings: config.Settings) -> server.Cognito:
    base = os.environ["ONCA_OAUTH_COGNITO_BASE_URL"].rstrip("/")
    client_id = os.environ["ONCA_OAUTH_COGNITO_CLIENT_ID"]

    def authorize_url(state: str, nonce: str, challenge: str) -> str:
        return base + "/oauth2/authorize?" + urlencode({
            "client_id": client_id, "response_type": "code",
            "scope": "openid email profile", "redirect_uri": settings.callback,
            "state": state, "nonce": nonce, "code_challenge": challenge,
            "code_challenge_method": "S256"})

    def exchange(code: str, verifier: str) -> dict[str, Any]:
        import requests
        # a PUBLIC Cognito client + PKCE: no client secret to hold (the AS leg is PKCE-bound)
        r = requests.post(base + "/oauth2/token", timeout=10, data={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": settings.callback, "code_verifier": verifier})
        r.raise_for_status()
        parts = str(r.json().get("id_token") or "").split(".")
        if len(parts) != 3:
            raise ValueError("no id_token")
        # fetched server-to-server from Cognito's token endpoint over TLS in this request —
        # trusted without a signature check (OIDC §3.1.3.7), as in Tarantula src/auth
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        if claims.get("aud") != client_id or claims.get("token_use") != "id":
            raise ValueError("unexpected id_token")
        return claims

    return server.Cognito(authorize_url=authorize_url, exchange=exchange)


def build() -> server.AuthServer:
    s = config.settings()
    return server.AuthServer(
        settings=s, store=DynamoStore(os.environ["ONCA_OAUTH_TABLE"]),
        sign_der=_sign_der, public_key_der=public_key_der(), cognito=_cognito(s),
        principal_of=principal_of)


def _request(event: dict[str, Any]) -> dict[str, Any]:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", "replace")
    form = dict(parse_qsl(body, keep_blank_values=True)) if "form-urlencoded" in (
        headers.get("content-type") or "") else {}
    cookies = {}
    for c in event.get("cookies") or []:
        k, _, v = c.partition("=")
        cookies[k.strip()] = v.strip()
    return {"method": ((event.get("requestContext") or {}).get("http") or {}).get("method", "GET"),
            "path": event.get("rawPath") or "/", "query": event.get("queryStringParameters") or {},
            "form": form, "headers": headers, "cookies": cookies}


FLOW = {"/oauth/authorize": "authorize", "/oauth/callback": "callback",
        "/oauth/consent": "consent", "/oauth/token": "token", "/oauth/revoke": "revoke"}
PRM_PREFIX = "/.well-known/oauth-protected-resource"
ASM = ("/.well-known/oauth-authorization-server/oauth", "/.well-known/oauth-authorization-server")


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    req = _request(event)
    path = req["path"].rstrip("/") or "/"
    if path == PRM_PREFIX or path.startswith(PRM_PREFIX + "/"):
        route, out = PRM_PREFIX, build().protected_resource_metadata(path[len(PRM_PREFIX):] or "/mcp")
    elif path in ASM:
        route, out = path, build().authorization_server_metadata()
    elif path == "/.well-known/agent-card.json":            # #186 A2A card (same behavior)
        from src.dashboard.discovery import render_agent_card
        route, out = path, {"status": 200, "cookies": [], "body": json.dumps(render_agent_card(), ensure_ascii=False),
                            "headers": {"Content-Type": "application/json", "Cache-Control": "public, max-age=3600",
                                        "Access-Control-Allow-Origin": "*"}}
    elif path == "/oauth/jwks.json":
        route, out = path, build().jwks()
    elif path in FLOW:
        route = path
        if killed():
            out = server.error_page("O acesso por apps está temporariamente indisponível.", 503)
        else:
            out = getattr(build(), FLOW[path])(req)
    else:
        route, out = "other", {"status": 404, "headers": {}, "cookies": [], "body": "Not found"}
    print("oauth: " + json.dumps({"route": route, "status": out["status"]}))   # T16
    resp = {"statusCode": out["status"], "headers": out.get("headers") or {},
            "body": out.get("body") or ""}
    if out.get("cookies"):
        resp["cookies"] = out["cookies"]
    return resp
