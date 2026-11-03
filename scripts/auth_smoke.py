"""Onça authentication smoke test across every sector tenant (2026-11-03).

No passwords and no accounts: each layer is exercised on its own — the Pre Token trigger and
the feed Lambda are invoked directly with the claims Cognito/the JWT authorizer would carry,
the Hosted UI and the authorizer over HTTPS. Users and federated links are reported as counts
per tenant; no email is ever printed.

  python -I scripts/auth_smoke.py      (AWS credentials for account 668449743071)

Covers: per tenant — password-login claims re-asserted, scoped feed (own tenant, items > 0,
zero out-of-scope entities, payload budget); fail-closed (no identity, unknown tenant, role
group only); industry-group fallback; every Google/Apple link resolves to a live tenant;
unmapped and self-changed-email federated users get no tenant; pool users' tenant claims are
live; Hosted UI (login page, Google, Apple, foreign redirect refused) on onssa.org and tenant
domains; the authorizer refuses missing/garbage/forged tokens on /api/feed and /api/ask; the
session broker with no / a forged refresh cookie.
"""
import base64, collections, gzip, json, sys, time, urllib.parse
import boto3, requests

R = "us-east-1"
lam = boto3.client("lambda", region_name=R); ddb = boto3.resource("dynamodb", region_name=R)
idp = boto3.client("cognito-idp", region_name=R)
FEED = "OncaPrototypeStack-OncaFeedApi086A4381-BG0SDNWBQ32T"
PRE = "OncaPrototypeStack-OncaPreTokenGenFn34C8181D-nCih2RAMwObb"
POOL, CLIENT, AUTH = "us-east-1_rxEkYhYHg", "4docsvvsocsujjqtj3aofduio3", "https://auth.onssa.org"
TCFG = ddb.Table("OncaPrototypeStack-OncaTenantConfigCADB28F8-IM6BJDHGUAPH")
results = []

def rec(area, name, ok, **d):
    results.append((area, name, bool(ok), d))
    print(f"{'PASS' if ok else 'FAIL'}  {area:9} {name:46} {json.dumps(d, ensure_ascii=False, default=str)[:190]}")

def feed(claims, headers=None):
    ev = {"version": "2.0", "rawPath": "/api/feed", "headers": headers or {"accept-encoding": "gzip"},
          "requestContext": {"http": {"method": "GET"}, **({"authorizer": {"jwt": {"claims": claims}}} if claims else {})}}
    r = lam.invoke(FunctionName=FEED, Payload=json.dumps(ev).encode()); p = r["Payload"].read()
    if r.get("FunctionError"): return {"statusCode": "LAMBDA_ERROR"}, None, len(p)
    o = json.loads(p); b = o.get("body") or ""
    try: body = json.loads(gzip.decompress(base64.b64decode(b)) if o.get("isBase64Encoded") else b) if b else None
    except Exception: body = None
    return o, body, len(p)

def leaks(body, modules):
    keep = {m.lower() for m in modules}; attrs = body.get("entity_attrs") or {}; groups = body.get("groups") or {}
    ok = {e for e, a in attrs.items() if {i.lower() for i in (a or {}).get("industries") or []} & keep}
    st = [e for e in ok if e in groups]
    while st:
        for c in groups.get(st.pop(), []):
            if c not in ok: ok.add(c); st.append(c) if c in groups else None
    return sorted(set(attrs) - ok)

# ---- 1. tenants: password-login claims -> pre-token -> scoped feed --------------------------
tenants = TCFG.scan()["Items"]
print(f"== {len(tenants)} tenants")
for t in sorted(tenants, key=lambda x: x["tenant_id"]):
    tid, tier, mods = t["tenant_id"], t.get("tier"), list(t.get("modules") or [])
    pev = {"triggerSource": "TokenGeneration_HostedAuth", "request": {"userAttributes": {
        "sub": "smoke", "email": "smoke@onca-smoke.invalid", "custom:tenant": tid, "custom:tier": tier or "saas"}}, "response": {}}
    out = json.loads(lam.invoke(FunctionName=PRE, Payload=json.dumps(pev).encode())["Payload"].read())
    ov = ((out.get("response") or {}).get("claimsOverrideDetails") or {}).get("claimsToAddOrOverride") or {}
    rec("pretoken", f"password user -> {tid}", ov.get("custom:tenant") == tid, tier=ov.get("custom:tier"))
    o, body, n = feed({"sub": "smoke", "custom:tenant": tid, "custom:tier": tier or ""})
    if o["statusCode"] == 200 and body:
        lk = leaks(body, mods) if tier != "entry" else []
        rec("feed", f"{tid} ({tier}, {len(mods)} mod)",
            body.get("tenant") == tid and len(body.get("feed") or []) > 0 and not lk and n < 4_500_000,
            items=len(body.get("feed") or []), payload=n, out_of_scope_entities=len(lk), tier=body.get("tier"), billing=body.get("billing"))
    else:
        rec("feed", f"{tid} ({tier}, {len(mods)} mod)", False, status=o["statusCode"], error=(body or {}).get("error"), billing=(body or {}).get("billing"))

# ---- 2. fail-closed + industry-group fallback -----------------------------------------------
o, b, _ = feed(None); rec("deny", "no identity -> 403", o["statusCode"] == 403, status=o["statusCode"])
o, b, _ = feed({"sub": "smoke", "custom:tenant": "no-such-tenant-smoke"}); rec("deny", "unknown tenant -> 403", o["statusCode"] == 403, status=o["statusCode"])
o, b, _ = feed({"sub": "smoke", "cognito:groups": "[operator admin]"}); rec("deny", "role group only -> 403", o["statusCode"] == 403, status=o["statusCode"])
for g in ("insurance", "banking"):
    o, b, n = feed({"sub": "smoke", "cognito:groups": f"[{g}]"})
    lk = leaks(b, [g]) if b else ["?"]
    rec("group", f"industry group [{g}] -> scoped", o["statusCode"] == 200 and b and b.get("tier") == "group" and not lk,
        status=o["statusCode"], items=len((b or {}).get("feed") or []), out_of_scope_entities=len(lk))

# ---- 3. federated (Google/Apple) mappings: each resolves to a live tenant ------------------
fed_tab = lam.get_function_configuration(FunctionName=PRE)["Environment"]["Variables"]["ONCA_FEDERATED_MAP_TABLE"]
links = [i for i in ddb.Table(fed_tab).scan()["Items"] if i.get("tenant_id") and "ttl" not in i]
live = {t["tenant_id"] for t in tenants}; per = collections.Counter(); bad = collections.Counter()
for i in links:
    k = i["email"]; attrs = {"cognito:user_status": "EXTERNAL_PROVIDER", "identities": '[{"providerName":"Google"}]', "email_verified": "true"}
    attrs.update({"sub": k[4:], "email": "x@onca-smoke.invalid"} if k.startswith("sub#") else {"sub": "smoke-none", "email": k})
    out = json.loads(lam.invoke(FunctionName=PRE, Payload=json.dumps({"triggerSource": "TokenGeneration_HostedAuth",
        "request": {"userAttributes": attrs}, "response": {}}).encode())["Payload"].read())
    got = (((out.get("response") or {}).get("claimsOverrideDetails") or {}).get("claimsToAddOrOverride") or {}).get("custom:tenant")
    per[i["tenant_id"]] += 1
    if got != i["tenant_id"] or i["tenant_id"] not in live: bad[i["tenant_id"]] += 1
rec("federated", f"{len(links)} Google/Apple links resolve to live tenants", not bad, per_tenant=dict(per), broken=dict(bad))
uev = {"cognito:user_status": "EXTERNAL_PROVIDER", "identities": '[{"providerName":"Google"}]', "email_verified": "true", "sub": "smoke-unmapped", "email": "nobody@onca-smoke.invalid"}
out = json.loads(lam.invoke(FunctionName=PRE, Payload=json.dumps({"request": {"userAttributes": uev}, "response": {}}).encode())["Payload"].read())
rec("federated", "unmapped Google user -> no tenant claim", not (out.get("response") or {}).get("claimsOverrideDetails"))
sev = dict(uev, email=next((i["email"] for i in links if not i["email"].startswith("sub#")), "x"), email_verified="false")
out = json.loads(lam.invoke(FunctionName=PRE, Payload=json.dumps({"request": {"userAttributes": sev}, "response": {}}).encode())["Payload"].read())
rec("federated", "self-changed email (unverified) -> no tenant", not (out.get("response") or {}).get("claimsOverrideDetails"))

# ---- 4. Cognito users: tenant claims point at live tenants ---------------------------------
users = [u for p in idp.get_paginator("list_users").paginate(UserPoolId=POOL) for u in p["Users"]]
ut = collections.Counter(); dangling = collections.Counter(); status = collections.Counter(u["UserStatus"] for u in users)
for u in users:
    a = {x["Name"]: x["Value"] for x in u.get("Attributes", [])}
    if a.get("custom:tenant"):
        ut[a["custom:tenant"]] += 1
        if a["custom:tenant"] not in live:
            gs = [x["GroupName"] for x in idp.admin_list_groups_for_user(UserPoolId=POOL, Username=u["Username"])["Groups"]]
            o, b, _ = feed({"sub": "smoke", "custom:tenant": a["custom:tenant"], "cognito:groups": "[" + " ".join(gs) + "]"})
            if o["statusCode"] != 200: dangling[a["custom:tenant"]] += 1
            else: rec("users", f"tenant '{a['custom:tenant']}' (no row) -> industry-group fallback", True, groups=len(gs), items=len((b or {}).get("feed") or []), status=u["UserStatus"])
rec("users", f"{len(users)} pool users, tenant claims live", not dangling, per_tenant=dict(ut), dangling=dict(dangling), status=dict(status))
yc = [u for u in users if any(x["Name"] == "email" and x["Value"].startswith("ycombinator@") for x in u.get("Attributes", []))]
if yc:
    a = {x["Name"]: x["Value"] for x in yc[0]["Attributes"]}
    rec("users", "YC reviewer account", a.get("custom:tenant") in live and yc[0]["Enabled"],
        tenant=a.get("custom:tenant"), status=yc[0]["UserStatus"], enabled=yc[0]["Enabled"])
else:
    rec("users", "YC reviewer account (not created yet)", True, note="skipped")

# ---- 5. HTTP: hosted UI, Google redirect, authorizer, session ------------------------------
def auth_url(redirect, **kw):
    q = {"client_id": CLIENT, "response_type": "code", "scope": "openid email", "redirect_uri": redirect,
         "code_challenge": "a" * 43, "code_challenge_method": "S256", **kw}
    return f"{AUTH}/oauth2/authorize?" + urllib.parse.urlencode(q)
for host in ("onssa.org", "tenant-test.onssa.org"):
    r = requests.get(auth_url(f"https://{host}/exec"), allow_redirects=False, timeout=15)
    rec("hostedui", f"authorize -> login page ({host})", r.status_code == 302 and "/login" in r.headers.get("location", ""), status=r.status_code)
    r = requests.get(auth_url(f"https://{host}/exec", identity_provider="Google"), allow_redirects=False, timeout=15)
    loc = urllib.parse.urlparse(r.headers.get("location", "")).hostname
    rec("hostedui", f"Google button -> accounts.google.com ({host})", r.status_code == 302 and loc == "accounts.google.com", status=r.status_code, to=loc)
r = requests.get(auth_url("https://onssa.org/exec", identity_provider="SignInWithApple"), allow_redirects=False, timeout=15)
loc = urllib.parse.urlparse(r.headers.get("location", "")).hostname
rec("hostedui", "Apple button -> appleid.apple.com", r.status_code == 302 and loc == "appleid.apple.com", status=r.status_code, to=loc)
r = requests.get(auth_url("https://evil.example/exec"), allow_redirects=False, timeout=15)
rec("hostedui", "foreign redirect_uri refused", "redirect_mismatch" in r.headers.get("location", ""), status=r.status_code)
forged = ".".join(base64.urlsafe_b64encode(json.dumps(x).encode()).decode().rstrip("=") for x in (
    {"alg": "RS256", "kid": "smoke"}, {"sub": "smoke", "custom:tenant": "owner-all-sectors", "iss": f"https://cognito-idp.{R}.amazonaws.com/{POOL}",
     "aud": CLIENT, "token_use": "id", "exp": int(time.time()) + 3600})) + ".c2lnbmF0dXJl"
for path in ("/api/feed", "/api/ask"):
    for label, hdr in (("no token", {}), ("garbage token", {"authorization": "Bearer abc"}), ("forged owner token", {"authorization": f"Bearer {forged}"})):
        m = requests.post if path == "/api/ask" else requests.get
        r = m(f"https://onssa.org{path}", headers=hdr, timeout=15, **({"json": {"q": "x"}} if path == "/api/ask" else {}))
        rec("authorizer", f"{path} {label} -> 401", r.status_code == 401, status=r.status_code)
r = requests.post("https://onssa.org/api/session/refresh", headers={"x-onca-session": "1"}, timeout=15)
rec("session", "refresh with no cookie -> no session", r.status_code == 200 and r.json().get("session") == "none", status=r.status_code)
r = requests.post("https://onssa.org/api/session/refresh", headers={"x-onca-session": "1", "cookie": "onca_rt=forged"}, timeout=25)
rec("session", "refresh with forged cookie -> 401", r.status_code == 401, status=r.status_code)

f = [x for x in results if not x[2]]
print(f"\n== {len(results) - len(f)}/{len(results)} passed")
for a, n, _, d in f: print("FAILED:", a, n, d)
