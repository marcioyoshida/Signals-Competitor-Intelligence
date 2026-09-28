#!/usr/bin/env python3
"""Live end-to-end check of Onça's MCP authorization server + resource servers (#181/#182/#186),
driven exactly as a desktop MCP client does it (ported from Tarantula scripts/oauth_e2e.py).

Uses the QA client's Client ID Metadata Document (https://onssa.org/docs/qa/oauth-test-client.json,
loopback redirect) and the two ADR 027 QA personas: ``admin`` (banking + fintech) and ``entry``
(agri-funds) — so the cross-tenant boundary is tested with two real tenants. Prints PASS/FAIL per
check and never prints a token, code or verifier.

  AWS_PROFILE=my2027 .venv/bin/python scripts/oauth_mcp_e2e.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

BASE = "https://onssa.org"
CLIENT_ID = BASE + "/docs/qa/oauth-test-client.json"
PORT = 8977
REDIRECT = "http://127.0.0.1:%d/callback" % PORT
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + detail) if detail else ""))
    return ok


def http(method: str, url: str, data: dict | None = None, headers: dict | None = None,
         form: bool = False) -> tuple[int, dict, str]:
    body, h = None, dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h["content-type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            h["content-type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def pkce() -> tuple[str, str]:
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def authorize(page, challenge: str, state: str, creds: dict, resource: str, scope: str,
              expect_consent: bool | None = None) -> dict:
    """Real loopback listener (Playwright route interception misses a 302 after a form POST)."""
    import http.server
    import threading

    captured: dict = {}

    class _CB(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            captured.setdefault("url", "http://127.0.0.1:%d" % PORT + self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    http.server.HTTPServer.allow_reuse_address = True
    srv = http.server.HTTPServer(("127.0.0.1", PORT), _CB)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    q = urllib.parse.urlencode({"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT,
                                "code_challenge": challenge, "code_challenge_method": "S256",
                                "state": state, "resource": resource, "scope": scope})
    page.goto(BASE + "/oauth/authorize?" + q, timeout=30_000)
    try:
        page.fill("#signInFormUsername:visible", creds["username"], timeout=8_000)
        page.fill("#signInFormPassword:visible", creds["password"])
        page.click('input[name="signInSubmitButton"]:visible')
    except Exception:
        pass   # a live Cognito session skips the form
    try:
        page.wait_for_selector("button.allow", timeout=15_000)
        body = page.content()
        if expect_consent is not False:
            check("consent page shown (T3)", True)
            check("consent shows the client's real host (T9)", "onssa.org" in body)
            check("consent warns about a localhost-only client (T10)", "neste computador" in body)
        page.click("button.allow")
    except Exception:
        if expect_consent:
            check("consent page shown (T3)", False, "no consent page")
    for _ in range(150):
        if "url" in captured:
            break
        page.wait_for_timeout(200)
    srv.server_close()
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(captured.get("url", "")).query))


def token_for(page, creds: dict, resource: str, scope: str, expect_consent: bool | None = None) -> dict:
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(12)
    cb = authorize(page, challenge, state, creds, resource, scope, expect_consent)
    if "code" not in cb:
        return {"_callback": cb}
    form = {"grant_type": "authorization_code", "code": cb["code"], "code_verifier": verifier,
            "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "resource": resource}
    s, _, b = http("POST", BASE + "/oauth/token", form, form=True)
    tok = json.loads(b) if s == 200 else {"_status": s, "_body": b[:200]}
    tok["_form"], tok["_callback"], tok["_state"] = form, cb, state
    return tok


def rpc(path: str, access: str | None, method: str, params: dict | None = None) -> tuple[int, dict, dict]:
    h = {"accept": "application/json, text/event-stream"}
    if access:
        h["authorization"] = "Bearer " + access
    s, headers, body = http("POST", BASE + path, {"jsonrpc": "2.0", "id": 1, "method": method,
                                                  "params": params or {}}, h)
    try:
        return s, headers, json.loads(body)
    except json.JSONDecodeError:
        return s, headers, {}


def tool(access: str, name: str, args: dict) -> tuple[dict, bool]:
    _, _, r = rpc("/mcp", access, "tools/call", {"name": name, "arguments": args})
    res = r.get("result") or {}
    if res.get("isError"):
        return {"error": (res.get("content") or [{}])[0].get("text")}, True
    return json.loads((res.get("content") or [{"text": "{}"}])[0]["text"]), False


def main() -> int:
    from playwright.sync_api import sync_playwright

    from qa_pipeline.lib import config

    personas = config.qa_credentials()["personas"]
    mcp_res, ops_res, a2a_res = BASE + "/mcp", BASE + "/mcp/ops", BASE + "/a2a"

    # ---- discovery as a client walks it -------------------------------------------------------
    s, h, _ = rpc("/mcp", None, "initialize")
    wa = {k.lower(): v for k, v in h.items()}.get("www-authenticate", "")
    check("unauthenticated initialize → 401 + WWW-Authenticate from the Lambda", s == 401 and "resource_metadata" in wa)
    s, _, b = http("GET", BASE + "/.well-known/oauth-protected-resource/mcp")
    check("protected resource metadata (/mcp)", s == 200 and json.loads(b)["resource"] == mcp_res)
    s, _, b = http("GET", BASE + "/.well-known/oauth-authorization-server/oauth")
    meta = json.loads(b) if s == 200 else {}
    check("AS metadata: S256 + CIMD + iss, no DCR", meta.get("code_challenge_methods_supported") == ["S256"]
          and meta.get("client_id_metadata_document_supported") is True and "registration_endpoint" not in meta)
    s, _, _ = http("GET", BASE + "/oauth/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": "https://evil.example/cb",
        "code_challenge": "x", "code_challenge_method": "S256"}))
    check("unregistered redirect_uri → error page, no redirect (T2)", s == 400)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        # ---- tenant A (admin persona: banking + fintech) ---------------------------------------
        page = browser.new_context().new_page()
        tok = token_for(page, personas["admin"], mcp_res, "onca:read")
        cb = tok.get("_callback", {})
        check("code redirect carries state and iss (T6)", cb.get("iss") == BASE + "/oauth" and "code" in cb)
        check("code → tokens (1 h access, rotating refresh)", tok.get("expires_in") == 3600 and bool(tok.get("refresh_token")),
              str(tok.get("_status", "")))
        s, _, b = http("POST", BASE + "/oauth/token", tok.get("_form", {}), form=True)
        check("replayed code rejected (T1)", s == 400 and json.loads(b).get("error") == "invalid_grant")
        access = tok.get("access_token", "")
        s, _, r = rpc("/mcp", access, "tools/list")
        names = [t["name"] for t in (r.get("result") or {}).get("tools", [])]
        check("tools/list (read tools)", s == 200 and names == ["lookup_entity", "entity_signals", "regulatory_events", "ask"], str(names))
        out, err = tool(access, "lookup_entity", {"query": "Itaú"})
        ents = out.get("entities") or []
        check("lookup_entity('Itaú') → non-empty registry row with provenance",
              not err and bool(ents) and ents[0]["entity"] == "itau" and bool(out.get("source")) and bool(out.get("as_of")),
              str(err and out))
        out, err = tool(access, "entity_signals", {"entity": "itau", "days": 30})
        sig = out.get("signals") or []
        check("entity_signals(itau) → non-empty dated rows, each with a source URL",
              not err and bool(sig) and all(x.get("url") for x in sig), "%d rows" % len(sig))
        out, err = tool(access, "regulatory_events", {"days": 30})
        ev = out.get("events") or []
        check("regulatory_events → non-empty, official act linked", not err and bool(ev) and bool(ev[0].get("url")),
              "%d events" % len(ev))
        out, err = tool(access, "ask", {"q": "Quais mudanças regulatórias recentes afetam bancos?"})
        check("ask → grounded answer with citations", not err and len(str(out.get("answer") or "")) > 20,
              str(out.get("error") or out.get("citations") and len(out["citations"])))
        s, _, _ = rpc("/mcp/ops", access, "tools/list")
        check("an /mcp token is refused at /mcp/ops (audience)", s == 401)
        s, _, _ = rpc("/mcp", "not-a-token", "tools/list")
        check("garbage token → 401 (T11)", s == 401)

        # ---- /mcp/ops: the admin QA persona IS in the Cognito `operator` group ---------------
        ops = token_for(page, personas["admin"], ops_res, "onca:write")
        oacc = ops.get("access_token", "")
        s, _, r = rpc("/mcp/ops", oacc, "tools/list")
        onames = {t["name"] for t in (r.get("result") or {}).get("tools", [])}
        check("operator → /mcp/ops lists the /api/act catalog, every tool needs idempotency_key",
              {"trigger_run", "record_decision", "list_product_radar"} <= onames and all(
                  "idempotency_key" in t["inputSchema"]["required"] for t in r["result"]["tools"]), str(len(onames)))
        key = "e2e-" + secrets.token_hex(6)
        s, _, r = rpc("/mcp/ops", oacc, "tools/call", {"name": "list_product_radar",
                                                        "arguments": {"idempotency_key": key}})
        first = (r.get("result") or {})
        s, _, r2 = rpc("/mcp/ops", oacc, "tools/call", {"name": "list_product_radar",
                                                         "arguments": {"idempotency_key": key}})
        body2 = json.loads(((r2.get("result") or {}).get("content") or [{"text": "{}"}])[0]["text"] or "{}")
        check("ops call runs through /api/act and a replay returns the stored result",
              not first.get("isError") and body2.get("idempotent_replay") is True, str(first)[:120])
        s, _, _ = rpc("/mcp", oacc, "tools/list")
        check("an /mcp/ops token is refused at /mcp (audience)", s == 401)

        # ---- A2A --------------------------------------------------------------------------------
        a2a = token_for(page, personas["admin"], a2a_res, "onca:read", expect_consent=False)
        s, _, r = rpc("/a2a", a2a.get("access_token"), "message/send", {"message": {
            "role": "user", "messageId": "e2e-1", "parts": [{"kind": "text", "text": "Quem está em alerta agora?"}]}})
        res = r.get("result") or {}
        check("A2A message/send → agent message", s == 200 and res.get("kind") == "message"
              and len(str((res.get("parts") or [{}])[0].get("text") or "")) > 10, str(r.get("error") or "")[:120])

        # ---- tenant B (entry persona: agri-funds only) — the cross-tenant boundary -------------
        page_b = browser.new_context().new_page()
        tb = token_for(page_b, personas["entry"], mcp_res, "onca:read")
        acc_b = tb.get("access_token", "")
        out, err = tool(acc_b, "lookup_entity", {"query": "Itaú"})
        ids_b = [e["entity"] for e in out.get("entities") or []]
        check("tenant B (agri-funds) cannot see the bank Itaú — only its own agri-funds entities",
              not err and "itau" not in ids_b and all(set(e["industries"]) & {"agri-funds"} for e in out.get("entities") or []),
              str(ids_b))
        ops_b = token_for(page_b, personas["entry"], ops_res, "onca:write", expect_consent=False)
        check("non-operator → /mcp/ops sign-in refused (access_denied, no code)",
              ops_b.get("_callback", {}).get("error") == "access_denied" and "access_token" not in ops_b,
              str(ops_b.get("_callback")))
        out, err = tool(acc_b, "entity_signals", {"entity": "itau"})
        check("tenant B entity_signals(itau) refused", err)
        out, _ = tool(acc_b, "regulatory_events", {"industry": "banking", "days": 60})
        check("tenant B sees no banking regulatory events", out.get("events") == [])

        # ---- refresh rotation + reuse detection (T12), revocation (T14) ------------------------
        rf = {"grant_type": "refresh_token", "refresh_token": tok.get("refresh_token", ""), "client_id": CLIENT_ID}
        s, _, b = http("POST", BASE + "/oauth/token", rf, form=True)
        t2 = json.loads(b) if s == 200 else {}
        check("refresh rotates the refresh token", s == 200 and t2.get("refresh_token") not in (None, tok.get("refresh_token")))
        s, _, _ = http("POST", BASE + "/oauth/token", rf, form=True)
        check("reused refresh token rejected (T12)", s == 400)
        s, _, _ = http("POST", BASE + "/oauth/token", dict(rf, refresh_token=t2.get("refresh_token", "")), form=True)
        check("…and reuse revoked the whole family (T12)", s == 400)
        deadline, s = time.time() + 75, 200
        while time.time() < deadline:
            s, _, _ = rpc("/mcp", t2.get("access_token", ""), "tools/list")
            if s == 401:
                break
            time.sleep(10)
        check("a revoked family's access token stops within ~60 s (T14)", s == 401)
        http("POST", BASE + "/oauth/revoke", {"token": tb.get("refresh_token", ""), "client_id": CLIENT_ID}, form=True)
        http("POST", BASE + "/oauth/revoke", {"token": a2a.get("refresh_token", ""), "client_id": CLIENT_ID}, form=True)
        browser.close()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
