"""ADR 030: the authenticated synthetic canary (every 4 h).

Exercises the paths a signed-in user takes, which no anonymous probe can see (both 2026-11-03
incidents were invisible to Fleet Monitor: a Google login 500 and a feed over Lambda's 6 MB
response cap). No password is involved: the feed Lambda is invoked directly with the same JWT
claims the HTTP API's authorizer forwards, and the pre-token trigger with a synthetic Google
sign-in event (an address on a reserved `.invalid` domain, which never maps to a tenant).

Each check returns pass/fail plus a measurement against a budget below the hard limit, so a
regression shows up as a failed check hours or weeks before users see an error:

  feed:<tenant>   status 200, own tenant, items > 0, Lambda payload < FEED_PAYLOAD_BUDGET
  pretoken        no error, billed duration < PRETOKEN_BUDGET_MS (Cognito allows 5 s)
  http:<path>     public routes answer as expected (page 200, API 401 unauthenticated)
  session         the broker reaches Cognito and refuses a dummy code (401) within budget
  app             the store app's /api/session/app route answers and app-guard.js is served (#312)
  stack           CloudFormation resources < STACK_RESOURCE_BUDGET (the hard limit is 500)

Emits `Onca/Canary` CanaryRun=1 and ChecksFailed=n (one alarm covers both a failure and a
canary that stopped running) and writes the report to s3://<digests>/ops/canary/latest.json for
Fleet Monitor. Costs no Bedrock call.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import UTC, datetime
from typing import Any, Callable

FEED_PAYLOAD_BUDGET = 4_500_000        # Lambda's response cap is 6 MB (6,291,556 bytes)
PRETOKEN_BUDGET_MS = 3_000             # Cognito gives a trigger 5 s, then retries
SESSION_BUDGET_S = 8.0                 # the broker waits up to 20 s; a healthy exchange is < 1 s
STACK_RESOURCE_BUDGET = 485            # CloudFormation's hard limit is 500 per stack
REPORT_KEY = "ops/canary/latest.json"
OPERATOR_ONLY_KEYS = ("mobile_usage", "tenant_usage")

Check = dict[str, Any]


def _check(name: str, ok: bool, **detail: Any) -> Check:
    return {"check": name, "ok": bool(ok), **detail}


def feed_event(tenant: str) -> dict[str, Any]:
    return {"version": "2.0", "rawPath": "/api/feed", "headers": {"accept-encoding": "gzip"},
            "requestContext": {"http": {"method": "GET"},
                               "authorizer": {"jwt": {"claims": {"sub": "onca-canary",
                                                                 "custom:tenant": tenant}}}}}


def check_feed(lam: Any, fn: str, tenant: str) -> Check:
    import gzip
    name = f"feed:{tenant}"
    try:
        raw = lam.invoke(FunctionName=fn, Payload=json.dumps(feed_event(tenant)).encode())
        payload = raw["Payload"].read()
        if raw.get("FunctionError"):
            return _check(name, False, error=raw["FunctionError"], payload_bytes=len(payload))
        out = json.loads(payload)
        body = out.get("body") or ""
        feed = json.loads(gzip.decompress(base64.b64decode(body)) if out.get("isBase64Encoded") else body)
        items = len(feed.get("feed") or [])
        # operator-only rollups must reach no tenant (#165/#288; #313: never the store reviewer)
        leaked = [k for k in OPERATOR_ONLY_KEYS if feed.get(k)]
        ok = (out.get("statusCode") == 200 and feed.get("tenant") == tenant and items > 0
              and len(payload) < FEED_PAYLOAD_BUDGET and not leaked)
        return _check(name, ok, status=out.get("statusCode"), items=items, payload_bytes=len(payload),
                      budget_bytes=FEED_PAYLOAD_BUDGET, leaked=leaked)
    except Exception as exc:  # noqa: BLE001 - a canary reports, never raises
        return _check(name, False, error=f"{type(exc).__name__}: {exc}"[:200])


PRETOKEN_EVENT = {
    "triggerSource": "TokenGeneration_HostedAuth",
    "request": {"userAttributes": {
        "sub": "onca-canary", "email": "canary@onca-canary.invalid", "email_verified": "true",
        "cognito:user_status": "EXTERNAL_PROVIDER", "identities": '[{"providerName":"Google"}]'}},
    "response": {},
}


def check_pretoken(lam: Any, fn: str) -> Check:
    try:
        r = lam.invoke(FunctionName=fn, Payload=json.dumps(PRETOKEN_EVENT).encode(), LogType="Tail")
        r["Payload"].read()
        log = base64.b64decode(r.get("LogResult") or "").decode("utf-8", "replace")
        m = re.search(r"Billed Duration: (\d+) ms", log)
        ms = int(m.group(1)) if m else None
        cold = "Init Duration" in log
        ok = not r.get("FunctionError") and ms is not None and ms < PRETOKEN_BUDGET_MS
        return _check("pretoken", ok, billed_ms=ms, cold=cold, budget_ms=PRETOKEN_BUDGET_MS,
                      error=r.get("FunctionError"))
    except Exception as exc:  # noqa: BLE001
        return _check("pretoken", False, error=f"{type(exc).__name__}: {exc}"[:200])


def check_http(get: Callable[..., Any], url: str, expect: int, min_bytes: int = 0) -> Check:
    name = "http:" + url.split("://", 1)[-1]
    try:
        t0 = time.monotonic()
        r = get(url, timeout=15, allow_redirects=False)
        secs = round(time.monotonic() - t0, 2)
        ok = r.status_code == expect and len(r.content) >= min_bytes
        return _check(name, ok, status=r.status_code, expect=expect, bytes=len(r.content), secs=secs)
    except Exception as exc:  # noqa: BLE001
        return _check(name, False, error=f"{type(exc).__name__}: {exc}"[:200])


def check_session(post: Callable[..., Any], origin: str) -> Check:
    """A dummy code must come back 401 ("code exchange refused"): proves broker -> Cognito
    /oauth2/token is up and fast. A 5xx or 504 is the 2026-11-03 failure mode."""
    try:
        t0 = time.monotonic()
        r = post(f"{origin}/api/session/exchange", timeout=25,
                 headers={"x-onca-session": "1", "content-type": "application/json"},
                 data=json.dumps({"code": "onca-canary", "code_verifier": "onca-canary",
                                  "redirect_uri": f"{origin}/exec"}))
        secs = round(time.monotonic() - t0, 2)
        return _check("session", r.status_code == 401 and secs < SESSION_BUDGET_S, status=r.status_code,
                      secs=secs, budget_s=SESSION_BUDGET_S)
    except Exception as exc:  # noqa: BLE001
        return _check("session", False, error=f"{type(exc).__name__}: {exc}"[:200])


def check_stack(cfn: Any, stack: str) -> Check:
    try:
        n = sum(len(p["StackResourceSummaries"])
                for p in cfn.get_paginator("list_stack_resources").paginate(StackName=stack))
        return _check("stack", n < STACK_RESOURCE_BUDGET, resources=n, budget=STACK_RESOURCE_BUDGET,
                      hard_limit=500)
    except Exception as exc:  # noqa: BLE001
        return _check("stack", False, error=f"{type(exc).__name__}: {exc}"[:200])


def check_app(http: Any, origin: str) -> Check:
    """#312/#318: the store app's health/version route answers, and app-guard.js (first script of
    every app-reachable page) is served to an app user agent."""
    try:
        r = http.post(f"{origin}/api/session/app", timeout=15,
                      headers={"x-onca-session": "1", "content-type": "application/json"},
                      data=json.dumps({"kind": "probe", "platform": "ios", "version": "0.0.1"}))
        body = r.json() if r.status_code == 200 else {}
        g = http.get(f"{origin}/v2/app-guard.js", timeout=15, headers={"user-agent": "OncaApp/canary"})
        ok = (r.status_code == 200 and body.get("update") in ("ok", "soft", "hard")
              and g.status_code == 200 and b"api/session/app" in g.content)
        return _check("app", ok, status=r.status_code, update=body.get("update"), guard=g.status_code)
    except Exception as exc:  # noqa: BLE001
        return _check("app", False, error=f"{type(exc).__name__}: {exc}"[:200])


def run(env: dict[str, str], lam: Any, cfn: Any, http: Any) -> list[Check]:
    origin = env.get("ONCA_CANARY_ORIGIN", "https://onssa.org").rstrip("/")
    checks: list[Check] = []
    for tenant in [t.strip() for t in env.get("ONCA_CANARY_TENANTS", "").split(",") if t.strip()]:
        checks.append(check_feed(lam, env["ONCA_CANARY_FEED_FN"], tenant))
    if env.get("ONCA_CANARY_PRETOKEN_FN"):
        checks.append(check_pretoken(lam, env["ONCA_CANARY_PRETOKEN_FN"]))
    checks.append(check_http(http.get, f"{origin}/exec", 200, min_bytes=10_000))
    checks.append(check_http(http.get, f"{origin}/api/feed", 401))
    for host in [h.strip() for h in env.get("ONCA_CANARY_EXTRA_HOSTS", "").split(",") if h.strip()]:
        checks.append(check_http(http.get, f"https://{host}/exec", 200, min_bytes=10_000))
    checks.append(check_session(http.post, origin))
    checks.append(check_app(http, origin))
    if env.get("ONCA_STACK_NAME"):
        checks.append(check_stack(cfn, env["ONCA_STACK_NAME"]))
    return checks


def lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    import boto3
    import requests

    env = dict(os.environ)
    checks = run(env, boto3.client("lambda"), boto3.client("cloudformation"), requests)
    failed = [c for c in checks if not c["ok"]]
    report = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "ok": not failed,
              "failed": [c["check"] for c in failed], "checks": checks}
    print(json.dumps(report, ensure_ascii=False))
    boto3.client("cloudwatch").put_metric_data(Namespace="Onca/Canary", MetricData=[
        {"MetricName": "CanaryRun", "Value": 1, "Unit": "Count"},
        {"MetricName": "ChecksFailed", "Value": len(failed), "Unit": "Count"}])
    if env.get("ONCA_DIGESTS_BUCKET"):
        boto3.client("s3").put_object(Bucket=env["ONCA_DIGESTS_BUCKET"], Key=REPORT_KEY,
                                      Body=json.dumps(report, ensure_ascii=False, indent=1).encode(),
                                      ContentType="application/json", CacheControl="no-cache")
    return report
