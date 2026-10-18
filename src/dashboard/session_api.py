"""Mobile session broker (#161/#164) — keeps the Cognito REFRESH token out of page JavaScript.

The dashboards log in with Cognito Hosted UI + PKCE (public SPA client). Before this module the
browser exchanged the code itself and kept only the 1-hour ID token in ``sessionStorage``: every
new tab — and every launch of an installed PWA, where iOS clears ``sessionStorage`` — meant a
fresh login, and there was no idle limit or server-side way to end a session.

Three POST routes on the Cognito HTTP API (no JWT authorizer — the cookie IS the credential):

* ``/api/session/exchange`` ``{code, code_verifier, redirect_uri}`` — the code exchange, done
  here; the refresh token goes into an ``HttpOnly; Secure; SameSite=Strict`` cookie scoped to
  ``/api/session`` (script can never read it), the ID token comes back in the body.
* ``/api/session/refresh`` — a new ID token from the cookie's refresh token. The cookie's
  ``Max-Age`` is the tenant's idle limit (``session_idle_days`` on onca-tenant-config, default
  ``DEFAULT_IDLE_DAYS``) and is re-issued on every refresh: a sliding inactivity window. The
  Cognito refresh-token validity (30 days, infra) is the absolute cap.
* ``/api/session/logout`` — revokes the refresh token at Cognito and expires the cookie.

Own sign-in form on /exec (#277, ported from Anteater a888626): Cognito's managed-login page
can't be resized (inputs/buttons 32px, checkbox 16px on a phone — ``spacingDensity`` only accepts
REGULAR), so /exec carries its own form and these routes do the password side; Google/Apple go
straight to the provider (``identity_provider=``) and come back through ``exchange``.

* ``/api/session/password`` ``{email, password}`` — ADMIN_USER_PASSWORD_AUTH (the app client
  allows it; the call needs THIS Lambda's IAM role, so it is not open to any Cognito API caller).
  Success = the same reply as ``exchange``; ``{challenge: "NEW_PASSWORD_REQUIRED", session}`` for an
  invited user's first sign-in. Wrong e-mail and wrong password answer the same 401; Cognito's own
  per-user lockout throttles guessing.
* ``/api/session/new-password`` ``{email, session, new_password}`` — answers that challenge.
* ``/api/session/forgot`` ``{email}`` — sends the reset code; always ``{ok: true}`` (no enumeration).
* ``/api/session/reset`` ``{email, code, new_password}`` — sets the new password.
Nothing here logs an e-mail or a password.

A Cognito global sign-out (operator ``revoke_user_sessions`` act, #164) invalidates the refresh
token, so the next ``refresh`` returns 401 and the client clears its per-user caches.

CSRF: every route requires the ``x-onca-session: 1`` header — a cross-site form cannot set it and
a cross-origin fetch that does gets preflighted (no CORS grant for it) — on top of SameSite=Strict.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.parse import urlparse

import requests

COOKIE = "onca_rt"
COOKIE_PATH = "/api/session"
DEFAULT_IDLE_DAYS = 7
MAX_IDLE_DAYS = 30  # = the Cognito refresh-token validity (infra/app.py)
# redirect_uri must be one the Cognito app client already allows; checked here too so this
# endpoint can't be used to exchange codes for arbitrary origins.
_ALLOWED_HOSTS = {"onssa.org", "www.onssa.org"}
# ADR 029 (#254/#255): the store app's native redirect. Exactly this URI — never a scheme wildcard.
APP_REDIRECT = "onca://auth"


def _resp(status: int, body: dict[str, Any], cookies: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, ensure_ascii=False),
    }
    if cookies:
        out["cookies"] = cookies
    return out


def _cookie(value: str, max_age: int) -> str:
    return (f"{COOKIE}={value}; Max-Age={max_age}; Path={COOKIE_PATH}; "
            "HttpOnly; Secure; SameSite=Strict")


def _read_cookie(event: dict[str, Any]) -> str | None:
    raw = list(event.get("cookies") or [])
    hdr = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}.get("cookie")
    if hdr:
        raw += str(hdr).split(";")
    for c in raw:
        k, _, v = str(c).strip().partition("=")
        if k == COOKIE and v:
            return v
    return None


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}


def _claims(id_token: str) -> dict[str, Any]:
    """Unverified decode of an ID token that came STRAIGHT from Cognito's token endpoint over
    TLS in this same request — used only to pick the tenant's idle limit, never to authorize."""
    try:
        seg = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except Exception:
        return {}


def idle_days(tenant: str | None, *, table: Any | None = None) -> int:
    """The tenant's re-auth-after-inactivity limit in days (#164), clamped to [1, MAX_IDLE_DAYS]."""
    days: Any = None
    if tenant:
        try:
            from src.dashboard import tenant_config

            days = (tenant_config.get_tenant_config(tenant, table=table) or {}).get("session_idle_days")
        except Exception as exc:  # pragma: no cover - config read is best-effort
            print(f"Warning: session idle-days lookup failed: {exc}")
    try:
        d = int(days) if days is not None else DEFAULT_IDLE_DAYS
    except (TypeError, ValueError):
        d = DEFAULT_IDLE_DAYS
    return max(1, min(MAX_IDLE_DAYS, d))


def _token_post(data: dict[str, str]) -> requests.Response:
    return requests.post(f"{os.environ['ONCA_COGNITO_DOMAIN']}/oauth2/token", data=data,
                         headers={"content-type": "application/x-www-form-urlencoded"}, timeout=10)


def _ok_redirect(uri: str) -> bool:
    if uri == APP_REDIRECT:
        return True
    u = urlparse(uri or "")
    extra = {h.strip() for h in os.environ.get("ONCA_SESSION_EXTRA_HOSTS", "").split(",") if h.strip()}
    return u.scheme == "https" and (u.hostname or "") in (_ALLOWED_HOSTS | extra)


def _session_reply(id_token: str, refresh_token: str, expires_in: Any) -> dict[str, Any]:
    days = idle_days((_claims(id_token).get("custom:tenant") or None))
    return _resp(200, {"id_token": id_token, "expires_in": expires_in, "idle_days": days},
                 [_cookie(refresh_token, days * 86400)])


def exchange(body: dict[str, Any]) -> dict[str, Any]:
    code, verifier, redirect = (str(body.get(k) or "") for k in ("code", "code_verifier", "redirect_uri"))
    if not code or not verifier or not _ok_redirect(redirect):
        return _resp(400, {"error": "code, code_verifier and an allowed redirect_uri are required"})
    r = _token_post({"grant_type": "authorization_code", "client_id": os.environ["ONCA_COGNITO_CLIENT_ID"],
                     "code": code, "code_verifier": verifier, "redirect_uri": redirect})
    if r.status_code != 200:
        return _resp(401, {"error": "code exchange refused"})
    j = r.json()
    if not j.get("id_token") or not j.get("refresh_token"):
        return _resp(502, {"error": "incomplete token response"})
    return _session_reply(j["id_token"], j["refresh_token"], j.get("expires_in"))


def refresh(event: dict[str, Any]) -> dict[str, Any]:
    rt = _read_cookie(event)
    if not rt:
        # never signed in on this device: an ordinary state, not an error (no console noise)
        return _resp(200, {"session": "none"})
    r = _token_post({"grant_type": "refresh_token", "client_id": os.environ["ONCA_COGNITO_CLIENT_ID"],
                     "refresh_token": rt})
    if r.status_code != 200 or not r.json().get("id_token"):
        # revoked (global sign-out), expired, or the idle window lapsed server-side: end it here
        return _resp(401, {"error": "session ended"}, [_cookie("", 0)])
    j = r.json()
    return _session_reply(j["id_token"], rt, j.get("expires_in"))


def logout(event: dict[str, Any]) -> dict[str, Any]:
    rt = _read_cookie(event)
    if rt:
        try:
            requests.post(f"{os.environ['ONCA_COGNITO_DOMAIN']}/oauth2/revoke",
                          data={"token": rt, "client_id": os.environ["ONCA_COGNITO_CLIENT_ID"]},
                          headers={"content-type": "application/x-www-form-urlencoded"}, timeout=10)
        except Exception as exc:  # pragma: no cover - the cookie is expired regardless
            print(f"Warning: refresh-token revoke failed: {exc}")
    return _resp(200, {"ok": True}, [_cookie("", 0)])


_NEW_PW = "NEW_PASSWORD_REQUIRED"


def _idp():
    import boto3
    return boto3.client("cognito-idp")


def _email(body: dict[str, Any]) -> str:
    e = str(body.get("email") or "").strip().lower()
    return e if 3 <= len(e) <= 254 and "@" in e else ""


def _err_name(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code") or type(exc).__name__)


def _auth_result(r: dict[str, Any]) -> dict[str, Any]:
    a = r.get("AuthenticationResult") or {}
    if a.get("IdToken") and a.get("RefreshToken"):
        return _session_reply(a["IdToken"], a["RefreshToken"], a.get("ExpiresIn"))
    if r.get("ChallengeName") == _NEW_PW and r.get("Session"):
        return _resp(200, {"challenge": _NEW_PW, "session": r["Session"]})
    # MFA or any other challenge: not configured on this pool (MfaConfiguration OFF), not offered
    return _resp(409, {"error": "unsupported_challenge"})


def _pw_error(exc: Exception) -> dict[str, Any]:
    name = _err_name(exc)
    if name == "InvalidPasswordException":
        return _resp(400, {"error": "weak_password"})
    if name in ("CodeMismatchException", "ExpiredCodeException"):
        return _resp(400, {"error": "bad_code"})
    if name in ("TooManyRequestsException", "LimitExceededException", "TooManyFailedAttemptsException"):
        return _resp(429, {"error": "too_many_attempts"})
    if name == "NotAuthorizedException" and "attempts exceeded" in str(exc).lower():
        return _resp(429, {"error": "too_many_attempts"})
    if name in ("NotAuthorizedException", "UserNotFoundException", "UserNotConfirmedException",
                "PasswordResetRequiredException", "InvalidParameterException"):
        return _resp(401, {"error": "invalid_credentials"})
    print(f"session: cognito error ({name})")
    return _resp(502, {"error": "unavailable"})


def password(body: dict[str, Any]) -> dict[str, Any]:
    email, pw = _email(body), str(body.get("password") or "")
    if not email or not pw or len(pw) > 256:
        return _resp(400, {"error": "email and password are required"})
    try:
        r = _idp().admin_initiate_auth(
            UserPoolId=os.environ["ONCA_USER_POOL_ID"], ClientId=os.environ["ONCA_COGNITO_CLIENT_ID"],
            AuthFlow="ADMIN_USER_PASSWORD_AUTH", AuthParameters={"USERNAME": email, "PASSWORD": pw})
    except Exception as exc:  # noqa: BLE001 - mapped to a generic answer
        return _pw_error(exc)
    return _auth_result(r)


def new_password(body: dict[str, Any]) -> dict[str, Any]:
    email, sess, pw = _email(body), str(body.get("session") or ""), str(body.get("new_password") or "")
    if not email or not sess or not pw or len(pw) > 256:
        return _resp(400, {"error": "email, session and new_password are required"})
    try:
        r = _idp().admin_respond_to_auth_challenge(
            UserPoolId=os.environ["ONCA_USER_POOL_ID"], ClientId=os.environ["ONCA_COGNITO_CLIENT_ID"],
            ChallengeName=_NEW_PW, Session=sess, ChallengeResponses={"USERNAME": email, "NEW_PASSWORD": pw})
    except Exception as exc:  # noqa: BLE001
        return _pw_error(exc)
    return _auth_result(r)


def forgot(body: dict[str, Any]) -> dict[str, Any]:
    email = _email(body)
    if email:
        try:
            _idp().forgot_password(ClientId=os.environ["ONCA_COGNITO_CLIENT_ID"], Username=email)
        except Exception as exc:  # noqa: BLE001 - same answer either way (no enumeration)
            if _pw_error(exc)["statusCode"] == 429:
                return _resp(429, {"error": "too_many_attempts"})
    return _resp(200, {"ok": True})


def reset(body: dict[str, Any]) -> dict[str, Any]:
    email, code, pw = _email(body), str(body.get("code") or "").strip(), str(body.get("new_password") or "")
    if not email or not code or not pw or len(pw) > 256:
        return _resp(400, {"error": "email, code and new_password are required"})
    try:
        _idp().confirm_forgot_password(ClientId=os.environ["ONCA_COGNITO_CLIENT_ID"],
                                       Username=email, ConfirmationCode=code, Password=pw)
    except Exception as exc:  # noqa: BLE001
        out = _pw_error(exc)
        return _resp(400, {"error": "bad_code"}) if out["statusCode"] == 401 else out
    return _resp(200, {"ok": True})


_BODY_ROUTES = {"exchange": exchange, "password": password, "new-password": new_password,
                "forgot": forgot, "reset": reset}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    if headers.get("x-onca-session") != "1":
        return _resp(403, {"error": "forbidden"})
    path = str(event.get("rawPath") or "").rstrip("/")
    route = path.rsplit("/", 1)[-1]
    if route in _BODY_ROUTES:
        return _BODY_ROUTES[route](_body(event))
    if path.endswith("/refresh"):
        return refresh(event)
    if path.endswith("/logout"):
        return logout(event)
    return _resp(404, {"error": "not found"})
