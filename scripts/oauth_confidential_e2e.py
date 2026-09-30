#!/usr/bin/env python3
"""Live end-to-end check of the PRE-REGISTERED CONFIDENTIAL client path (#184 Copilot Studio
"Manual" OAuth, #186 Gemini Enterprise), everything short of the Microsoft/Google side.

Copilot Studio's Manual mode sends a client id + secret, may omit PKCE and may omit ``resource``.
This registers a THROWAWAY confidential client (secret held in memory only, never printed or
written), drives authorize → consent → code → token exactly that way with the two ADR 027 QA
personas, and deletes the registration at the end (also on failure). The redirect is an https
URL on onssa.org that doesn't exist: the code is read from the browser's URL, as Copilot's
``global.consent.azure-apim.net`` callback would receive it.

  AWS_PROFILE=my2027 .venv/bin/python scripts/oauth_confidential_e2e.py
"""
from __future__ import annotations

import base64
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import oauth_mcp_e2e as e2e  # noqa: E402  (shared http/rpc/tool/check helpers)
from oauth_register_client import _table  # noqa: E402

BASE = e2e.BASE
REDIRECT = BASE + "/docs/qa/oauth-confidential-callback"
check, http, rpc, tool = e2e.check, e2e.http, e2e.rpc, e2e.tool


def _authorize(page, client_id: str, creds: dict, *, challenge: str | None = None) -> dict:
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
         "state": secrets.token_urlsafe(12), "scope": "onca:read"}           # no resource
    if challenge:
        q.update(code_challenge=challenge, code_challenge_method="S256")
    page.goto(BASE + "/oauth/authorize?" + urllib.parse.urlencode(q), timeout=30_000)
    try:
        page.fill("#signInFormUsername:visible", creds["username"], timeout=8_000)
        page.fill("#signInFormPassword:visible", creds["password"])
        page.click('input[name="signInSubmitButton"]:visible')
    except Exception:
        pass   # a live Cognito session skips the form
    consent = ""
    try:
        page.wait_for_selector("button.allow", timeout=15_000)
        consent = page.content()
        page.click("button.allow")
    except Exception:
        pass
    try:
        page.wait_for_url(REDIRECT + "?*", timeout=20_000)
    except Exception:
        pass
    cb = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(page.url).query)) if page.url.startswith(REDIRECT) else {}
    cb["_consent"] = consent
    cb["_state_ok"] = cb.get("state") == q["state"]
    return cb


def _basic(cid: str, secret: str) -> dict:
    raw = "%s:%s" % (urllib.parse.quote(cid, safe=""), urllib.parse.quote(secret, safe=""))
    return {"authorization": "Basic " + base64.b64encode(raw.encode()).decode()}


def main() -> int:
    from playwright.sync_api import sync_playwright

    from qa_pipeline.lib import config
    from src.dashboard.oauth import tokens

    personas = config.qa_credentials()["personas"]
    table = _table()
    cid, secret = "onca-e2e-" + secrets.token_hex(6), secrets.token_urlsafe(32)
    table.put_item(Item={"pk": "client#" + cid, "sk": "-", "client_id": cid,
                         "client_name": "Copilot Studio (teste e2e)", "redirect_uris": [REDIRECT],
                         "confidential": True, "secret_sha256": tokens.sha256_hex(secret),
                         "default_resource": "/mcp", "created": int(time.time()), "e2e": True})
    tok: dict = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context().new_page()

            # ---- tenant A, Manual mode: no PKCE, no resource, secret via HTTP Basic -----------
            cb = _authorize(page, cid, personas["admin"])
            check("confidential client, no PKCE, no resource → code on the registered https callback",
                  "code" in cb and cb["_state_ok"] and cb.get("iss") == BASE + "/oauth", str({k: v for k, v in cb.items() if k in ("error", "error_description")}))
            check("consent names the registered client", "Copilot Studio (teste e2e)" in cb["_consent"])
            form = {"grant_type": "authorization_code", "code": cb.get("code", ""), "redirect_uri": REDIRECT}
            s, _, _ = http("POST", BASE + "/oauth/token", dict(form, client_id=cid), form=True)
            check("token without the secret refused", s in (400, 401), str(s))
            s, _, _ = http("POST", BASE + "/oauth/token", form, _basic(cid, secret[:-2] + "xx"), form=True)
            check("token with a wrong secret refused", s in (400, 401), str(s))
            s, _, b = http("POST", BASE + "/oauth/token", form, _basic(cid, secret), form=True)
            tok = e2e.json.loads(b) if s == 200 else {}
            check("code + secret (HTTP Basic) → tokens (1 h access, refresh)",
                  s == 200 and tok.get("expires_in") == 3600 and bool(tok.get("refresh_token")), str(s))
            access = tok.get("access_token", "")
            s, _, r = rpc("/mcp", access, "tools/list")
            names = [t["name"] for t in (r.get("result") or {}).get("tools", [])]
            check("default resource = /mcp: tools/list works", s == 200 and "lookup_entity" in names, str(names))
            s, _, _ = rpc("/mcp/ops", access, "tools/list")
            check("…and the token is refused at /mcp/ops", s == 401)
            out, err = tool(access, "lookup_entity", {"query": "Itaú"})
            ents = out.get("entities") or []
            check("lookup_entity('Itaú') → non-empty row with provenance",
                  not err and bool(ents) and ents[0]["entity"] == "itau" and bool(out.get("as_of")), str(err and out))
            out, err = tool(access, "ask", {"q": "Quais mudanças regulatórias recentes afetam bancos?"})
            check("ask → grounded answer", not err and len(str(out.get("answer") or "")) > 20, str(out.get("error") or ""))

            # ---- refresh: the secret is required here too (client_secret_post) ----------------
            rf = {"grant_type": "refresh_token", "refresh_token": tok.get("refresh_token", ""), "client_id": cid}
            s, _, _ = http("POST", BASE + "/oauth/token", rf, form=True)
            check("refresh without the secret refused", s in (400, 401), str(s))
            s, _, b = http("POST", BASE + "/oauth/token", dict(rf, client_secret=secret), form=True)
            t2 = e2e.json.loads(b) if s == 200 else {}
            check("refresh with the secret (post) rotates", s == 200 and t2.get("refresh_token") not in (None, tok.get("refresh_token")), str(s))
            tok = t2 or tok

            # ---- a connector that DOES send PKCE still works -----------------------------------
            verifier, challenge = e2e.pkce()
            cb = _authorize(page, cid, personas["admin"], challenge=challenge)
            s, _, b = http("POST", BASE + "/oauth/token", {"grant_type": "authorization_code", "code": cb.get("code", ""),
                                                          "redirect_uri": REDIRECT, "code_verifier": verifier,
                                                          "client_id": cid, "client_secret": secret}, form=True)
            check("PKCE + secret also accepted", s == 200, str(s))
            if s == 200:
                http("POST", BASE + "/oauth/revoke", {"token": e2e.json.loads(b).get("refresh_token", ""),
                                                      "client_id": cid, "client_secret": secret}, form=True)

            # ---- tenant B through the SAME connector: the boundary holds ----------------------
            page_b = browser.new_context().new_page()
            cb = _authorize(page_b, cid, personas["entry"])
            s, _, b = http("POST", BASE + "/oauth/token", {"grant_type": "authorization_code", "code": cb.get("code", ""),
                                                          "redirect_uri": REDIRECT}, _basic(cid, secret), form=True)
            tb = e2e.json.loads(b) if s == 200 else {}
            out, err = tool(tb.get("access_token", ""), "lookup_entity", {"query": "Itaú"})
            ids_b = [e["entity"] for e in out.get("entities") or []]
            check("tenant B via the same client cannot see Itaú", s == 200 and not err and "itau" not in ids_b, str(ids_b))
            out, err = tool(tb.get("access_token", ""), "entity_signals", {"entity": "itau"})
            check("tenant B entity_signals(itau) refused", err)
            if tb.get("refresh_token"):
                http("POST", BASE + "/oauth/revoke", {"token": tb["refresh_token"], "client_id": cid,
                                                      "client_secret": secret}, form=True)
            browser.close()
    finally:
        table.delete_item(Key={"pk": "client#" + cid, "sk": "-"})
    # ---- revocation: a deleted registration can't refresh --------------------------------------
    s, _, _ = http("POST", BASE + "/oauth/token", {"grant_type": "refresh_token", "refresh_token": tok.get("refresh_token", ""),
                                                  "client_id": cid, "client_secret": secret}, form=True)
    check("after the registration is deleted, refresh is refused", s in (400, 401), str(s))

    failed = [n for n, ok, _ in e2e.RESULTS if not ok]
    print("\n%d/%d passed" % (len(e2e.RESULTS) - len(failed), len(e2e.RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
