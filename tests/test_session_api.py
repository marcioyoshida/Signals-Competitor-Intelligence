"""#161/#164: the session broker keeps the refresh token in an httpOnly cookie."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.dashboard import session_api as sa


class _R:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


def _jwt(claims):
    import base64
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{seg}.s"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ONCA_COGNITO_DOMAIN", "https://auth.example")
    monkeypatch.setenv("ONCA_COGNITO_CLIENT_ID", "cid")
    monkeypatch.setattr(sa, "idle_days", lambda tenant, **k: 5 if tenant == "acme" else 7)


def _ev(path, body=None, cookie=None, hdr=True):
    ev = {"rawPath": path, "headers": {"x-onca-session": "1"} if hdr else {},
          "body": json.dumps(body or {})}
    if cookie:
        ev["cookies"] = [f"{sa.COOKIE}={cookie}"]
    return ev


def test_requires_the_csrf_header():
    assert sa.lambda_handler(_ev("/api/session/refresh", hdr=False), None)["statusCode"] == 403


def test_exchange_sets_httponly_cookie_with_tenant_idle_window(monkeypatch):
    posted = {}
    monkeypatch.setattr(sa, "_token_post", lambda d: posted.update(d) or _R(200, {
        "id_token": _jwt({"sub": "u1", "custom:tenant": "acme"}), "refresh_token": "RT", "expires_in": 3600}))
    resp = sa.lambda_handler(_ev("/api/session/exchange", {
        "code": "c", "code_verifier": "v", "redirect_uri": "https://onssa.org/exec"}), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["id_token"] and "refresh_token" not in body and body["idle_days"] == 5
    ck = resp["cookies"][0]
    assert ck.startswith("onca_rt=RT;") and "HttpOnly" in ck and "Secure" in ck
    assert "SameSite=Strict" in ck and "Path=/api/session" in ck and "Max-Age=432000" in ck
    assert posted["grant_type"] == "authorization_code" and posted["code_verifier"] == "v"


def test_exchange_refuses_a_foreign_redirect_uri(monkeypatch):
    monkeypatch.setattr(sa, "_token_post", lambda d: pytest.fail("must not call Cognito"))
    resp = sa.lambda_handler(_ev("/api/session/exchange", {
        "code": "c", "code_verifier": "v", "redirect_uri": "https://evil.example/exec"}), None)
    assert resp["statusCode"] == 400


def test_exchange_accepts_exactly_the_store_app_redirect(monkeypatch):
    # ADR 029 (#254): the app's system-browser sign-in comes back on onca://auth; nothing else on
    # that scheme (or any other custom scheme) is an allowed redirect.
    monkeypatch.setattr(sa, "_token_post", lambda d: _R(200, {
        "id_token": _jwt({"sub": "u1"}), "refresh_token": "RT", "expires_in": 3600}))
    ok = sa.lambda_handler(_ev("/api/session/exchange", {
        "code": "c", "code_verifier": "v", "redirect_uri": "onca://auth"}), None)
    assert ok["statusCode"] == 200
    for bad in ("onca://evil", "onca://auth/x", "evil://auth", "onca:auth"):
        monkeypatch.setattr(sa, "_token_post", lambda d: pytest.fail("must not call Cognito"))
        r = sa.lambda_handler(_ev("/api/session/exchange", {
            "code": "c", "code_verifier": "v", "redirect_uri": bad}), None)
        assert r["statusCode"] == 400, bad


def test_refresh_slides_the_cookie_and_ended_session_expires_it(monkeypatch):
    monkeypatch.setattr(sa, "_token_post", lambda d: _R(200, {"id_token": _jwt({"sub": "u1"})}))
    ok = sa.lambda_handler(_ev("/api/session/refresh", cookie="RT"), None)
    assert ok["statusCode"] == 200 and "Max-Age=604800" in ok["cookies"][0]
    # global sign-out / expiry: Cognito refuses the refresh token
    monkeypatch.setattr(sa, "_token_post", lambda d: _R(400, {"error": "invalid_grant"}))
    gone = sa.lambda_handler(_ev("/api/session/refresh", cookie="RT"), None)
    assert gone["statusCode"] == 401 and "Max-Age=0" in gone["cookies"][0]
    none = sa.lambda_handler(_ev("/api/session/refresh"), None)
    assert none["statusCode"] == 200 and json.loads(none["body"]) == {"session": "none"}


def test_logout_revokes_and_expires(monkeypatch):
    revoked = []
    monkeypatch.setattr(sa.requests, "post", lambda url, **k: revoked.append((url, k["data"])))
    resp = sa.lambda_handler(_ev("/api/session/logout", cookie="RT"), None)
    assert resp["statusCode"] == 200 and "Max-Age=0" in resp["cookies"][0]
    assert revoked[0][0].endswith("/oauth2/revoke") and revoked[0][1]["token"] == "RT"


def test_idle_days_clamped(monkeypatch):
    from src.dashboard import tenant_config
    monkeypatch.undo()
    for v, want in ((None, 7), ("3", 3), (0, 1), (400, 30), ("x", 7)):
        monkeypatch.setattr(tenant_config, "get_tenant_config",
                            lambda t, table=None, v=v: {"session_idle_days": v} if v is not None else {})
        assert sa.idle_days("t") == want


def _evr(route, body=None, csrf=True):
    """#277 own sign-in form: the password routes (ported from Anteater a888626)."""
    return _ev(f"/api/session/{route}", body, hdr=csrf)


# --- own sign-in page (2026-10-14): the password side runs in the broker ---------------------

class CE(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg)
        self.response = {"Error": {"Code": code, "Message": msg}}


@pytest.fixture
def idp(monkeypatch):
    monkeypatch.setenv("ONCA_USER_POOL_ID", "pool")
    st = {"calls": [], "resp": {"AuthenticationResult": {
        "IdToken": _jwt({"custom:tenant": "acme"}), "RefreshToken": "RT2", "ExpiresIn": 3600}}, "raise": None}

    class C:
        def __getattr__(self, name):
            def f(**kw):
                st["calls"].append((name, kw))
                if st["raise"]:
                    raise st["raise"]
                return st["resp"]
            return f
    monkeypatch.setattr(sa, "_idp", lambda: C())
    return st


def test_password_signs_in_with_the_same_cookie_reply_as_exchange(idp):
    r = sa.lambda_handler(_evr("password", {"email": " Ana@Acme.com ", "password": "pw-123456789012"}), None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["id_token"] and "RT2" not in r["body"]
    assert any(c.startswith(f"{sa.COOKIE}=RT2;") and "HttpOnly" in c for c in r["cookies"])
    name, kw = idp["calls"][0]
    assert name == "admin_initiate_auth" and kw["AuthFlow"] == "ADMIN_USER_PASSWORD_AUTH"
    assert kw["AuthParameters"]["USERNAME"] == "ana@acme.com" and kw["UserPoolId"] == "pool"


@pytest.mark.parametrize("code", ["NotAuthorizedException", "UserNotFoundException"])
def test_wrong_email_and_wrong_password_answer_the_same(idp, code):
    idp["raise"] = CE(code, "Incorrect username or password.")
    r = sa.lambda_handler(_evr("password", {"email": "a@b.co", "password": "x"}), None)
    assert r["statusCode"] == 401 and json.loads(r["body"]) == {"error": "invalid_credentials"}
    assert "a@b.co" not in r["body"]


def test_lockout_is_reported_as_too_many_attempts(idp):
    idp["raise"] = CE("NotAuthorizedException", "Password attempts exceeded")
    assert sa.lambda_handler(_evr("password", {"email": "a@b.co", "password": "x"}), None)["statusCode"] == 429


def test_invited_user_gets_the_new_password_challenge_then_signs_in(idp):
    idp["resp"] = {"ChallengeName": "NEW_PASSWORD_REQUIRED", "Session": "SESS"}
    r = sa.lambda_handler(_evr("password", {"email": "a@b.co", "password": "temp"}), None)
    assert json.loads(r["body"]) == {"challenge": "NEW_PASSWORD_REQUIRED", "session": "SESS"}
    assert not r.get("cookies")
    idp["resp"] = {"AuthenticationResult": {"IdToken": _jwt({}), "RefreshToken": "RT3", "ExpiresIn": 3600}}
    r = sa.lambda_handler(_evr("new-password", {"email": "a@b.co", "session": "SESS",
                                             "new_password": "a-long-new-password"}), None)
    assert r["statusCode"] == 200 and r["cookies"][0].startswith(f"{sa.COOKIE}=RT3;")
    name, kw = idp["calls"][-1]
    assert name == "admin_respond_to_auth_challenge"
    assert kw["ChallengeResponses"] == {"USERNAME": "a@b.co", "NEW_PASSWORD": "a-long-new-password"}


def test_weak_new_password_is_named(idp):
    idp["raise"] = CE("InvalidPasswordException")
    r = sa.lambda_handler(_evr("new-password", {"email": "a@b.co", "session": "S", "new_password": "short"}), None)
    assert r["statusCode"] == 400 and json.loads(r["body"])["error"] == "weak_password"


def test_other_challenges_are_not_offered(idp):
    idp["resp"] = {"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "S"}
    assert sa.lambda_handler(_evr("password", {"email": "a@b.co", "password": "x"}), None)["statusCode"] == 409


def test_forgot_never_reveals_whether_the_account_exists(idp):
    for exc in (None, CE("UserNotFoundException"), CE("InvalidParameterException")):
        idp["raise"] = exc
        r = sa.lambda_handler(_evr("forgot", {"email": "who@x.co"}), None)
        assert r["statusCode"] == 200 and json.loads(r["body"]) == {"ok": True}


def test_reset_with_a_bad_code(idp):
    idp["raise"] = CE("CodeMismatchException")
    r = sa.lambda_handler(_evr("reset", {"email": "a@b.co", "code": "1", "new_password": "p"}), None)
    assert r["statusCode"] == 400 and json.loads(r["body"])["error"] == "bad_code"
    idp["raise"] = None
    r = sa.lambda_handler(_evr("reset", {"email": "a@b.co", "code": "123456", "new_password": "a-long-pw-12"}), None)
    assert r["statusCode"] == 200
    assert idp["calls"][-1][0] == "confirm_forgot_password"


def test_new_routes_need_the_csrf_header_and_input(idp):
    assert sa.lambda_handler(_evr("password", {"email": "a@b.co", "password": "x"}, csrf=False), None)["statusCode"] == 403
    assert sa.lambda_handler(_evr("password", {"email": "nope", "password": "x"}), None)["statusCode"] == 400
    assert not idp["calls"]


def test_session_lambda_may_run_the_admin_password_flow():
    stack = (Path(__file__).resolve().parents[1] / "infra" / "app.py").read_text(encoding="utf-8")
    assert 'session_fn.add_environment("ONCA_USER_POOL_ID", user_pool.user_pool_id)' in stack
    assert '"cognito-idp:AdminInitiateAuth"' in stack and '"cognito-idp:AdminRespondToAuthChallenge"' in stack
    for route in ("password", "new-password", "forgot", "reset"):
        assert f'"/api/session/{route}"' in stack, route
