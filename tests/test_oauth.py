"""#181 — Onça's MCP authorization server (ported from Tarantula #99), tested threat by threat.
Each test names the Tarantula threat-model ID (T1–T21) it proves, plus Onça's own rules:
provisioned users only, three audience-bound resources, operator-only write scope."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import base64
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src.dashboard.oauth import apps, cimd, config, resource, server, tokens
from src.dashboard.oauth.store import MemoryStore

S = config.Settings()
CID = "https://client.example.com/mcp-client.json"
REDIRECT = "http://127.0.0.1:33418/callback"
KEY = ec.generate_private_key(ec.SECP256R1())
PUB = KEY.public_key().public_bytes(serialization.Encoding.DER,
                                    serialization.PublicFormat.SubjectPublicKeyInfo)


def sign_der(msg: bytes) -> bytes:
    return KEY.sign(msg, ec.ECDSA(hashes.SHA256()))


def doc(client_id=CID, uris=(REDIRECT,), name="Test Client"):
    return json.dumps({"client_id": client_id, "client_name": name,
                       "redirect_uris": list(uris)}).encode()


class Clock:
    def __init__(self):
        # real "now": minted tokens are verified by PyJWT against the wall clock
        self.t = float(__import__("time").time())

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    store, clock = MemoryStore(), Clock()
    seen = {}
    docs = {CID: doc()}
    principals = {"sub-1": {"sub": "sub-1", "tenant": "acme", "groups": [], "modules": ["banking"], "elevated": False}}

    def authorize_url(state, nonce, challenge):
        seen.update(state=state, nonce=nonce, challenge=challenge)
        return "https://auth.onssa.test/oauth2/authorize?state=" + state

    def exchange(code, verifier):
        assert tokens.pkce_s256(verifier) == seen["challenge"]   # our Cognito-leg PKCE
        return {"sub": seen.get("sub", "sub-1"), "nonce": seen.get("force_nonce", seen["nonce"])}

    def fetch(url):
        if url not in docs:
            raise cimd.CIMDError("client_id document could not be fetched")
        return cimd.validate_document(url, docs[url])

    srv = server.AuthServer(settings=S, store=store, sign_der=sign_der, public_key_der=PUB,
                            cognito=server.Cognito(authorize_url, exchange),
                            principal_of=lambda sub: principals.get(sub), fetch_cimd=fetch, now=clock)
    resource._family_cache.clear()
    return type("E", (), dict(srv=srv, store=store, clock=clock, seen=seen, docs=docs,
                              principals=principals))


VERIFIER = "v" * 64


def authz(e, **over):
    q = dict(response_type="code", client_id=CID, redirect_uri=REDIRECT,
             code_challenge=tokens.pkce_s256(VERIFIER), code_challenge_method="S256",
             state="st-1", resource=S.resource_uri("/mcp"), scope=config.READ)
    q.update(over)
    return e.srv.authorize({"method": "GET", "query": {k: v for k, v in q.items() if v is not None}})


def signin(e, **over):
    r = authz(e, **over)
    assert r["status"] == 302 and r["headers"]["Location"].startswith("https://auth.onssa.test/")
    return e.srv.callback({"query": {"code": "cog-code", "state": e.seen["state"]}})


def consent_parts(page):
    req = re.search(r'name=req value="([^"]+)"', page["body"]).group(1)
    csrf = re.search(r'name=csrf value="([^"]+)"', page["body"]).group(1)
    return req, csrf


def allow(e, page, **hdr):
    req, csrf = consent_parts(page)
    return e.srv.consent({"method": "POST", "headers": dict({"origin": S.base,
                                                             "sec-fetch-site": "same-origin"}, **hdr),
                          "form": {"req": req, "csrf": csrf, "decision": "allow"},
                          "cookies": {server.CSRF_COOKIE: csrf}})


def code_of(r):
    q = parse_qs(urlsplit(r["headers"]["Location"]).query)
    assert q["iss"] == [S.issuer] and q["state"] == ["st-1"]         # T6
    return q["code"][0]


def full_code(e):
    r = signin(e)
    # consent is remembered after the first approval (T3): later sign-ins go straight
    # to a code redirect
    return code_of(r if r["status"] == 302 else allow(e, r))


def token(e, **f):
    return e.srv.token({"method": "POST", "form": f})


def exchange_code(e, code, **over):
    f = dict(grant_type="authorization_code", code=code, code_verifier=VERIFIER,
             client_id=CID, redirect_uri=REDIRECT, resource=S.resource_uri("/mcp"))
    f.update(over)
    return token(e, **f)


def keys():
    return {tokens.kid_for(PUB): PUB}


def who(e, access):
    return resource.principal({"authorization": "Bearer " + access}, path="/mcp", settings=S,
                              public_keys=keys(), family_lookup=e.store.get)


# ---- discovery -------------------------------------------------------------------------

def test_metadata_advertises_what_the_spec_requires(env):
    m = json.loads(env.srv.authorization_server_metadata()["body"])
    assert m["issuer"] == S.issuer                                    # T6 exact issuer
    assert m["code_challenge_methods_supported"] == ["S256"]          # T1 + client MUST
    assert m["client_id_metadata_document_supported"] is True         # D3
    assert "registration_endpoint" not in m                           # no DCR (owner)
    assert m["authorization_response_iss_parameter_supported"] is True
    prm = json.loads(env.srv.protected_resource_metadata()["body"])
    assert prm["resource"] == "https://onssa.org/mcp"
    assert prm["authorization_servers"] == [S.issuer]
    jwk = json.loads(env.srv.jwks()["body"])["keys"][0]
    assert jwk["alg"] == "ES256" and jwk["crv"] == "P-256" and "d" not in jwk


# ---- the happy path --------------------------------------------------------------------

def test_full_flow_issues_a_token_the_resource_server_accepts(env):
    r = exchange_code(env, full_code(env))
    body = json.loads(r["body"])
    assert r["status"] == 200 and r["headers"]["Cache-Control"] == "no-store"      # T7
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600
    p, why = who(env, body["access_token"])
    assert why == "" and p["sub"] == "sub-1" and p["tenant"] == "acme"


# ---- T1 authorization code interception -----------------------------------------------

@pytest.mark.parametrize("over", [dict(code_challenge=None), dict(code_challenge_method="plain"),
                                  dict(code_challenge_method=None)])
def test_T1_pkce_s256_is_required(env, over):
    r = authz(env, **over)
    q = parse_qs(urlsplit(r["headers"]["Location"]).query)
    assert q["error"] == ["invalid_request"] and q["iss"] == [S.issuer]


def test_T1_wrong_verifier_replay_and_expiry_are_rejected(env):
    c = full_code(env)
    assert json.loads(exchange_code(env, c, code_verifier="w" * 64)["body"])["error"] == "invalid_grant"
    assert json.loads(exchange_code(env, c)["body"])["error"] == "invalid_grant"   # consumed
    c2 = full_code(env)
    assert exchange_code(env, c2)["status"] == 200
    assert json.loads(exchange_code(env, c2)["body"])["error"] == "invalid_grant"  # replay
    c3 = full_code(env)
    env.clock.t += 61
    assert json.loads(exchange_code(env, c3)["body"])["error"] == "invalid_grant"  # > 60 s


def test_T1_code_is_bound_to_client_and_redirect(env):
    assert json.loads(exchange_code(env, full_code(env), client_id="https://x.example/c.json")
                      ["body"])["error"] == "invalid_grant"
    assert json.loads(exchange_code(env, full_code(env), redirect_uri="http://127.0.0.1:1/callback")
                      ["body"])["error"] == "invalid_grant"


# ---- T2 open redirect -----------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "http://127.0.0.1:33418/callback/extra", "http://127.0.0.1:33418/callback?x=1",
    "https://evil.example/callback", "http://127.0.0.1:33418/callback#f",
    "http://user@127.0.0.1:33418/callback", "http://10.0.0.1:33418/callback"])
def test_T2_unregistered_redirects_render_an_error_and_never_redirect(env, bad):
    r = authz(env, redirect_uri=bad)
    assert r["status"] == 400 and "Location" not in r["headers"]


def test_T2_loopback_port_may_vary_but_https_must_match_exactly():
    assert cimd.redirect_allowed("http://127.0.0.1:5555/callback", [REDIRECT])     # OAuth 2.1
    assert cimd.redirect_allowed("http://localhost:9/cb", ["http://localhost:1/cb"])
    assert not cimd.redirect_allowed("https://a.example:8443/cb", ["https://a.example/cb"])
    assert not cimd.redirect_allowed("https://a.example/cb/", ["https://a.example/cb"])


# ---- T3 confused deputy / consent -----------------------------------------------------

def test_T3_first_authorization_always_shows_consent_then_is_remembered(env):
    page = signin(env)
    assert page["status"] == 200 and "Permitir acesso à Onça?" in page["body"]
    code_of(allow(env, page))
    again = signin(env)                          # same org, client, scope, redirect host
    assert again["status"] == 302 and "code=" in again["headers"]["Location"]


def test_T3_changed_client_document_or_redirect_host_reprompts(env):
    code_of(allow(env, signin(env)))
    env.docs[CID] = doc(name="Test Client v2")
    env.store.delete("cimd#" + tokens.sha256_hex(CID))
    assert signin(env)["status"] == 200


def test_T3_cognito_leg_state_nonce_and_replay(env):
    authz(env)
    req_id = env.seen["state"].split(".")[0]
    assert env.srv.callback({"query": {"code": "x", "state": req_id + ".forged"}})["status"] == 400
    env.seen["force_nonce"] = "other"
    assert env.srv.callback({"query": {"code": "x", "state": env.seen["state"]}})["status"] == 400
    env.seen.pop("force_nonce")
    assert env.srv.callback({"query": {"code": "x", "state": env.seen["state"]}})["status"] == 200
    # the same Cognito callback replayed after sign-in: refused
    assert env.srv.callback({"query": {"code": "x", "state": env.seen["state"]}})["status"] == 400


# ---- T4 CSRF, T5 clickjacking ---------------------------------------------------------

def test_T4_consent_post_needs_matching_csrf_cookie_and_same_origin(env):
    page = signin(env)
    req, csrf = consent_parts(page)
    base = {"method": "POST", "form": {"req": req, "csrf": csrf, "decision": "allow"}}
    assert env.srv.consent(dict(base, headers={}, cookies={}))["status"] == 403
    assert env.srv.consent(dict(base, headers={}, cookies={server.CSRF_COOKIE: "x"}))["status"] == 403
    assert env.srv.consent(dict(base, headers={"sec-fetch-site": "cross-site"},
                                cookies={server.CSRF_COOKIE: csrf}))["status"] == 403
    assert env.srv.consent(dict(base, headers={"origin": "https://evil.example"},
                                cookies={server.CSRF_COOKIE: csrf}))["status"] == 403
    assert env.srv.consent(dict(base, headers={"origin": "null"},
                                cookies={server.CSRF_COOKIE: csrf}))["status"] == 403
    # and the page itself must not make the browser send Origin: null (found live)
    assert page["headers"]["Referrer-Policy"] == "same-origin"
    assert env.srv.consent(dict(base, method="GET", headers={}, cookies={}))["status"] == 405
    assert allow(env, page)["status"] == 302


def test_T5_consent_and_error_pages_cannot_be_framed(env):
    for page in (signin(env), server.error_page("x")):
        h = page["headers"]
        assert h["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in h["Content-Security-Policy"]


# ---- T6 iss on errors -----------------------------------------------------------------

def test_T6_denial_redirect_carries_iss(env):
    page = signin(env)
    req, csrf = consent_parts(page)
    r = env.srv.consent({"method": "POST", "headers": {"origin": S.base},
                         "form": {"req": req, "csrf": csrf, "decision": "deny"},
                         "cookies": {server.CSRF_COOKIE: csrf}})
    q = parse_qs(urlsplit(r["headers"]["Location"]).query)
    assert q["error"] == ["access_denied"] and q["iss"] == [S.issuer] and q["state"] == ["st-1"]


# ---- T7 token handling ----------------------------------------------------------------

def test_T7_bearer_only_from_the_authorization_header(env):
    assert resource.bearer({"authorization": "Bearer abc"}) == "abc"
    assert resource.bearer({"authorization": "Basic abc"}) is None
    assert resource.bearer({}) is None


def test_T7_only_hashes_of_codes_and_refresh_tokens_are_stored(env):
    body = json.loads(exchange_code(env, full_code(env))["body"])
    dump = json.dumps(list(env.store.items.values()), default=str)
    assert body["refresh_token"] not in dump and body["access_token"] not in dump


# ---- T8 CIMD SSRF ---------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://client.example.com/c.json", "https://client.example.com", "https://client.example.com/",
    "https://user:pw@client.example.com/c.json", "https://client.example.com/c.json#x",
    "https://client.example.com:8443/c.json", "https://10.0.0.1/c.json",
    "https://[::1]/c.json", "https://localhost/c.json"])
def test_T8_bad_client_id_urls_are_refused_before_any_network(url):
    with pytest.raises(cimd.CIMDError):
        cimd.fetch(url, resolve=lambda h: pytest.fail("resolved"),
                   fetcher=lambda *a: pytest.fail("fetched"))


@pytest.mark.parametrize("addr", ["10.1.2.3", "127.0.0.1", "169.254.169.254", "100.64.0.1",
                                  "192.168.1.1", "::1", "fc00::1", "fe80::1", "0.0.0.0", "224.0.0.1"])
def test_T8_private_and_metadata_addresses_are_refused_before_connecting(addr):
    with pytest.raises(cimd.CIMDError):
        cimd.fetch(CID, resolve=lambda h: ["8.8.8.8", addr],
                   fetcher=lambda *a: pytest.fail("connected"))


def test_T8_document_rules():
    ok = lambda status, body: cimd.fetch(CID, resolve=lambda h: ["93.184.216.34"],  # noqa: E731
                                         fetcher=lambda ip, h, p, d: (status, body))
    assert ok(200, doc())["client_name"] == "Test Client"
    for status, body in [(302, doc()), (404, doc()), (200, b"x" * 6000), (200, b"not json"),
                         (200, doc(client_id="https://client.example.com/other.json")),
                         (200, doc(uris=("javascript:alert(1)",))),
                         (200, doc(uris=("http://evil.example/cb",))),
                         (200, json.dumps({"client_id": CID, "redirect_uris": [REDIRECT]}).encode())]:
        with pytest.raises(cimd.CIMDError):
            ok(status, body)


def test_T8_the_fetch_connects_to_the_checked_address():
    seen = {}
    cimd.fetch(CID, resolve=lambda h: ["93.184.216.34"],
               fetcher=lambda ip, host, path, dl: (seen.update(ip=ip, host=host, path=path)
                                                   or (200, doc())))
    assert seen == {"ip": "93.184.216.34", "host": "client.example.com", "path": "/mcp-client.json"}


# ---- T9 impersonation, T10 localhost --------------------------------------------------

def test_T9_the_real_host_is_shown_and_no_badge_unless_allowlisted(env):
    env.docs[CID] = doc(name="Claude")
    page = signin(env)["body"]
    assert "client.example.com" in page and "verificado" not in page
    env.srv.s = config.Settings(verified_hosts=frozenset({"client.example.com"}))
    env.store.delete("cimd#" + tokens.sha256_hex(CID))
    assert "✓ verificado" in signin(env)["body"]


def test_T10_loopback_only_clients_get_a_warning(env):
    assert "<b>neste computador</b>" in signin(env)["body"]
    env.docs[CID] = doc(uris=("https://client.example.com/cb",))
    env.store.delete("cimd#" + tokens.sha256_hex(CID))
    assert "neste computador" not in signin(env, redirect_uri="https://client.example.com/cb")["body"]


# ---- T11 / T20 token validation -------------------------------------------------------

def _jwt(claims, header=None, key=KEY):
    h = header or {"alg": "ES256", "kid": tokens.kid_for(PUB)}
    si = tokens.b64u(json.dumps(h).encode()) + "." + tokens.b64u(json.dumps(claims).encode())
    return si + "." + tokens.b64u(tokens.der_to_raw(key.sign(si.encode(), ec.ECDSA(hashes.SHA256()))))


def _claims(env, **over):
    c = tokens.access_claims(settings=S, resource_path="/mcp", sub="sub-1", tenant="acme", groups=[],
                             client_id=CID, scope=config.READ, family_id="f1",
                             now=int(__import__("time").time()))
    c.update(over)
    return c


@pytest.mark.parametrize("make,reason", [
    (lambda e: _jwt(_claims(e, aud="https://onssa.org/mcp/ops")), "aud"),
    (lambda e: _jwt(_claims(e, iss="https://auth.onssa.test")), "iss"),
    (lambda e: _jwt(_claims(e, exp=1, iat=0, nbf=0)), "expired"),
    (lambda e: _jwt(_claims(e), key=ec.generate_private_key(ec.SECP256R1())), "signature"),
    (lambda e: _jwt(_claims(e), header={"alg": "ES256", "kid": "nope"}), "kid"),
    (lambda e: tokens.b64u(b'{"alg":"none"}') + "." + tokens.b64u(json.dumps(_claims(e)).encode()) + ".", "alg"),
    (lambda e: _jwt({k: v for k, v in _claims(e).items() if k != "exp"}), "claims"),
])
def test_T11_T20_tokens_not_minted_for_this_resource_are_rejected(env, make, reason):
    env.store.put("usr#sub-1", "fam#f1", {"client_id": CID, "revoked": False})
    p, why = who(env, make(env))
    assert p is None and why == reason


def test_T11_a_cognito_id_token_is_rejected(env):
    cognito_like = tokens.b64u(b'{"alg":"RS256","kid":"cog"}') + "." + tokens.b64u(
        json.dumps({"iss": "https://cognito-idp.us-east-1.amazonaws.com/x", "aud": "client",
                    "token_use": "id", "sub": "sub-1"}).encode()) + ".sig"
    assert who(env, cognito_like) == (None, "alg")


def test_T11_hs256_signed_with_the_public_key_is_rejected(env):
    import hmac as _h, hashlib
    si = tokens.b64u(b'{"alg":"HS256","kid":"%s"}' % tokens.kid_for(PUB).encode()) + "." + \
        tokens.b64u(json.dumps(_claims(env)).encode())
    tok = si + "." + tokens.b64u(_h.new(PUB, si.encode(), hashlib.sha256).digest())
    assert who(env, tok) == (None, "alg")


# ---- T12 refresh reuse, T13 tier, lifetimes -------------------------------------------

def test_T12_refresh_rotates_and_reuse_kills_the_family(env):
    first = json.loads(exchange_code(env, full_code(env))["body"])
    r2 = json.loads(token(env, grant_type="refresh_token", refresh_token=first["refresh_token"],
                          client_id=CID)["body"])
    assert r2["refresh_token"] != first["refresh_token"]
    reuse = token(env, grant_type="refresh_token", refresh_token=first["refresh_token"], client_id=CID)
    assert json.loads(reuse["body"])["error"] == "invalid_grant"
    after = token(env, grant_type="refresh_token", refresh_token=r2["refresh_token"], client_id=CID)
    assert json.loads(after["body"])["error"] == "invalid_grant"          # family dead
    resource._family_cache.clear()
    assert who(env, r2["access_token"]) == (None, "revoked")


def test_T13_entitlement_is_server_side_and_refresh_rereads_it(env):
    first = json.loads(exchange_code(env, full_code(env))["body"])
    assert who(env, first["access_token"])[0]["tenant"] == "acme"
    env.principals["sub-1"] = dict(env.principals["sub-1"], tenant="acme2")      # moved tenant
    r2 = json.loads(token(env, grant_type="refresh_token", refresh_token=first["refresh_token"],
                          client_id=CID)["body"])
    assert who(env, r2["access_token"])[0]["tenant"] == "acme2"
    env.principals["sub-1"] = dict(env.principals["sub-1"], modules=[])          # de-provisioned
    r3 = token(env, grant_type="refresh_token", refresh_token=r2["refresh_token"], client_id=CID)
    assert json.loads(r3["body"])["error"] == "invalid_grant"                    # no new token
    resource._family_cache.clear()
    assert who(env, r2["access_token"]) == (None, "revoked")                     # and family dead


def test_refresh_lifetimes_30d_absolute_7d_idle(env):
    first = json.loads(exchange_code(env, full_code(env))["body"])
    env.clock.t += 7 * 86400 + 1
    idle = token(env, grant_type="refresh_token", refresh_token=first["refresh_token"], client_id=CID)
    assert json.loads(idle["body"])["error"] == "invalid_grant"
    rt = json.loads(exchange_code(env, full_code(env))["body"])["refresh_token"]
    for _ in range(6):                                  # used every 6 days: day 36 > 30
        env.clock.t += 6 * 86400
        r = token(env, grant_type="refresh_token", refresh_token=rt, client_id=CID)
        if r["status"] != 200:
            break
        rt = json.loads(r["body"])["refresh_token"]
    assert json.loads(r["body"])["error"] == "invalid_grant"               # 30 d absolute


def test_refresh_is_bound_to_its_client(env):
    first = json.loads(exchange_code(env, full_code(env))["body"])
    r = token(env, grant_type="refresh_token", refresh_token=first["refresh_token"],
              client_id="https://other.example/c.json")
    assert json.loads(r["body"])["error"] == "invalid_grant"


# ---- T14 revocation -------------------------------------------------------------------

def test_T14_revoking_an_app_kills_its_access_tokens_and_consent(env):
    first = json.loads(exchange_code(env, full_code(env))["body"])
    assert who(env, first["access_token"])[0]
    listed = apps.list_apps(env.store, "sub-1")
    assert [a["client_id"] for a in listed] == [CID] and listed[0]["sessions"] == 1
    assert apps.revoke_app(env.store, "sub-1", CID) == 1
    resource._family_cache.clear()
    assert who(env, first["access_token"]) == (None, "revoked")
    assert signin(env)["status"] == 200                                   # consent asked again
    assert apps.list_apps(env.store, "sub-1") == []


def test_rfc7009_revoke_always_200(env):
    first = json.loads(exchange_code(env, full_code(env))["body"])
    assert env.srv.revoke({"form": {"token": first["refresh_token"]}})["status"] == 200
    assert env.srv.revoke({"form": {"token": "unknown"}})["status"] == 200
    resource._family_cache.clear()
    assert who(env, first["access_token"]) == (None, "revoked")


# ---- T16 logging, T18 edge secret ------------------------------------------------------

def test_T16_handler_logs_route_and_status_only(env, monkeypatch, capsys):
    from src.dashboard.oauth import handler
    monkeypatch.setattr(handler, "build", lambda: env.srv)
    monkeypatch.delenv("ONCA_OAUTH_KILL_PARAM", raising=False)
    code = full_code(env)
    body = "grant_type=authorization_code&code=%s&code_verifier=%s&client_id=%s&redirect_uri=%s" % (
        code, VERIFIER, CID, REDIRECT)
    r = handler.lambda_handler({"rawPath": "/oauth/token", "body": body,
                                "headers": {"content-type": "application/x-www-form-urlencoded"},
                                "requestContext": {"http": {"method": "POST"}}})
    tok = json.loads(r["body"])
    out = capsys.readouterr().out
    assert r["statusCode"] == 200 and '"route": "/oauth/token"' in out
    for secret in (code, VERIFIER, tok["access_token"], tok["refresh_token"]):
        assert secret not in out


def test_kill_switch_refuses_flows_but_keeps_metadata(env, monkeypatch):
    from src.dashboard.oauth import handler
    monkeypatch.setattr(handler, "build", lambda: env.srv)
    monkeypatch.setattr(handler, "killed", lambda: True)
    assert handler.lambda_handler({"rawPath": "/oauth/authorize", "headers": {}})["statusCode"] == 503
    for p in ("/.well-known/oauth-authorization-server/oauth",
              "/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource/mcp/ops"):
        assert handler.lambda_handler({"rawPath": p, "headers": {}})["statusCode"] == 200


# ---- scope, resource, T21 -------------------------------------------------------------

def test_only_our_scope_and_resource_are_accepted(env):
    q = parse_qs(urlsplit(authz(env, scope="onca:read admin")["headers"]["Location"]).query)
    assert q["error"] == ["invalid_scope"]
    q = parse_qs(urlsplit(authz(env, resource="https://onssa.org/api")["headers"]["Location"]).query)
    assert q["error"] == ["invalid_target"]
    assert json.loads(exchange_code(env, full_code(env), resource="https://evil.example")
                      ["body"])["error"] == "invalid_target"


def test_T21_the_user_comes_only_from_the_token(env):
    env.seen["sub"] = "sub-A"
    env.principals["sub-A"] = dict(env.principals["sub-1"], sub="sub-A")
    tok = json.loads(exchange_code(env, full_code(env))["body"])["access_token"]
    p, _ = resource.principal({"authorization": "Bearer " + tok, "x-org": "sub-B"}, path="/mcp",
                              settings=S, public_keys=keys(), family_lookup=env.store.get)
    assert p["sub"] == "sub-A"


def test_challenge_header_points_clients_at_the_metadata():
    c = resource.challenge(S, "/mcp", "invalid_token")
    assert 'resource_metadata="https://onssa.org/.well-known/oauth-protected-resource/mcp"' in c
    assert 'scope="onca:read"' in c and 'error="invalid_token"' in c


def test_metadata_and_consent_link_the_legal_pages(env):
    # #98: RFC 8414 op_policy_uri / op_tos_uri, and the consent page links both
    m = json.loads(env.srv.authorization_server_metadata()["body"])
    assert m["op_policy_uri"] == "https://onssa.org/docs/privacy.html"
    assert m["op_tos_uri"] == "https://onssa.org/docs/terms.html"
    page = signin(env, **{})
    assert 'href="https://onssa.org/docs/privacy.html"' in page["body"]
    assert 'href="https://onssa.org/docs/terms.html"' in page["body"]


# ---- Onça: provisioned users only, audience-bound resources, operator-only write -------------

def test_unprovisioned_user_gets_no_code(env):
    env.principals["sub-1"] = dict(env.principals["sub-1"], modules=[])
    r = signin(env)
    assert r["status"] == 403 and "licença" in r["body"] and "Location" not in r["headers"]
    env.principals.pop("sub-1")
    assert signin(env)["status"] == 403


def test_write_scope_is_operator_only_and_rechecked_on_refresh(env):
    ops = S.resource_uri("/mcp/ops")
    r = signin(env, resource=ops, scope=config.WRITE)
    q = parse_qs(urlsplit(r["headers"]["Location"]).query)
    assert q["error"] == ["access_denied"]                                       # not an operator
    env.principals["sub-1"] = dict(env.principals["sub-1"], elevated=True, groups=["operator"])
    r = signin(env, resource=ops, scope=config.WRITE)
    code = code_of(r if r["status"] == 302 else allow(env, r))
    tok = json.loads(exchange_code(env, code, resource=ops)["body"])
    p, why = resource.principal({"authorization": "Bearer " + tok["access_token"]}, path="/mcp/ops",
                                settings=S, public_keys=keys(), family_lookup=env.store.get)
    assert why == "" and p["groups"] == ["operator"]
    # an /mcp/ops token is NOT accepted at /mcp (audience), nor the reverse
    assert who(env, tok["access_token"]) == (None, "aud")
    env.principals["sub-1"] = dict(env.principals["sub-1"], elevated=False, groups=[])
    again = token(env, grant_type="refresh_token", refresh_token=tok["refresh_token"], client_id=CID)
    assert json.loads(again["body"])["error"] == "invalid_grant"


def test_scope_must_match_the_resource(env):
    q = parse_qs(urlsplit(authz(env, resource=S.resource_uri("/mcp"), scope=config.WRITE)["headers"]["Location"]).query)
    assert q["error"] == ["invalid_scope"]


def test_every_resource_has_metadata(env):
    for path, scope in config.RESOURCES.items():
        m = json.loads(env.srv.protected_resource_metadata(path)["body"])
        assert m["resource"] == "https://onssa.org" + path and m["scopes_supported"] == [scope]
    assert env.srv.protected_resource_metadata("/nope")["status"] == 404
