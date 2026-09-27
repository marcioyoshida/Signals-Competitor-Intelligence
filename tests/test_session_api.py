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
