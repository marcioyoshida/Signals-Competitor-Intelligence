"""ADR 030: monitoring — user-path alarms, feed size watch, the authenticated canary, limit guards."""
from __future__ import annotations

import base64
import gzip
import io
import json
import re
from pathlib import Path

from src.dashboard import canary, feed_api, feed_builder

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "infra" / "app.py").read_text(encoding="utf-8")
MON = APP[APP.index("# --- ADR 030: monitoring"):APP.index('CfnOutput(self, "CanaryFunctionName"')]


# ---- canary -------------------------------------------------------------------------------

class _Lam:
    def __init__(self, feeds=None, pretoken_log="", error=None):
        self.feeds, self.pretoken_log, self.error, self.calls = feeds or {}, pretoken_log, error, []

    def invoke(self, FunctionName, Payload, LogType=None):
        ev = json.loads(Payload)
        self.calls.append(FunctionName)
        if FunctionName == "pre":
            return {"Payload": io.BytesIO(json.dumps(ev).encode()),
                    "LogResult": base64.b64encode(self.pretoken_log.encode()).decode(),
                    **({"FunctionError": self.error} if self.error else {})}
        tenant = ev["requestContext"]["authorizer"]["jwt"]["claims"]["custom:tenant"]
        body = json.dumps({"tenant": tenant, "feed": [{}] * self.feeds.get(tenant, 0)}).encode()
        out = {"statusCode": 200, "isBase64Encoded": True, "headers": {"content-encoding": "gzip"},
               "body": base64.b64encode(gzip.compress(body)).decode()}
        return {"Payload": io.BytesIO(json.dumps(out).encode())}


class _R:
    def __init__(self, status, content=b""):
        self.status_code, self.content = status, content

    def json(self):
        return json.loads(self.content or b"{}")


class _Http:
    def __init__(self, routes, session=401):
        self.routes, self.session, self.posts = routes, session, []

    def get(self, url, **kw):
        return self.routes.get(url, _R(404))

    def post(self, url, **kw):
        self.posts.append((url, kw))
        if url.endswith("/api/session/app"):
            return _R(200, b'{"ok": true, "update": "ok"}')
        return _R(self.session, b'{"error": "code exchange refused"}')


class _Cfn:
    def __init__(self, n):
        self.n = n

    def get_paginator(self, name):
        n = self.n

        class P:
            def paginate(self, StackName):
                return [{"StackResourceSummaries": [{}] * n}]
        return P()


ENV = {"ONCA_CANARY_FEED_FN": "feed", "ONCA_CANARY_PRETOKEN_FN": "pre",
       "ONCA_CANARY_TENANTS": "owner-all-sectors,directory-reviewer", "ONCA_STACK_NAME": "S",
       "ONCA_CANARY_EXTRA_HOSTS": "tenant-test.onssa.org"}
PAGE = _R(200, b"x" * 20_000)
ROUTES = {"https://onssa.org/exec": PAGE, "https://onssa.org/api/feed": _R(401, b"{}"),
          "https://tenant-test.onssa.org/exec": PAGE,
          "https://onssa.org/v2/app-guard.js": _R(200, b"... /api/session/app ...")}
WARM = "REPORT RequestId: x Duration: 9.1 ms Billed Duration: 10 ms Memory Size: 512 MB"


def _by(checks):
    return {c["check"]: c for c in checks}


def test_healthy_run_passes_every_check():
    checks = canary.run(ENV, _Lam({"owner-all-sectors": 736, "directory-reviewer": 375}, WARM),
                        _Cfn(478), _Http(ROUTES))
    assert all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]
    assert set(_by(checks)) == {"feed:owner-all-sectors", "feed:directory-reviewer", "pretoken",
                                "http:onssa.org/exec", "http:onssa.org/api/feed",
                                "http:tenant-test.onssa.org/exec", "session", "app", "stack"}


def test_the_two_2026_11_03_incidents_fail_the_canary():
    slow = "REPORT Duration: 7496.19 ms Billed Duration: 7591 ms Init Duration: 94.11 ms"
    lam = _Lam({"owner-all-sectors": 736, "directory-reviewer": 375}, slow)
    real = lam.invoke

    def too_big(FunctionName, Payload, LogType=None):        # the 6 MB response-cap failure
        if FunctionName == "feed" and "owner-all-sectors" in Payload.decode():
            return {"FunctionError": "Unhandled", "Payload": io.BytesIO(b'{"errorType":"Runtime.ExitError"}')}
        return real(FunctionName, Payload, LogType)
    lam.invoke = too_big
    c = _by(canary.run(ENV, lam, _Cfn(478), _Http(ROUTES)))
    assert not c["feed:owner-all-sectors"]["ok"] and c["feed:directory-reviewer"]["ok"]
    assert not c["pretoken"]["ok"] and c["pretoken"]["billed_ms"] == 7591 and c["pretoken"]["cold"]


def test_budgets_sit_below_the_hard_limits():
    assert canary.FEED_PAYLOAD_BUDGET < 6_291_556
    assert canary.PRETOKEN_BUDGET_MS < 5_000
    assert canary.STACK_RESOURCE_BUDGET < 500


def test_a_feed_past_the_payload_budget_fails_before_it_breaks():
    lam = _Lam({"owner-all-sectors": 1})
    real = lam.invoke

    def big(FunctionName, Payload, LogType=None):
        r = real(FunctionName, Payload, LogType)
        r["Payload"] = io.BytesIO(r["Payload"].read() + b" " * canary.FEED_PAYLOAD_BUDGET)
        return r
    lam.invoke = big
    assert not canary.check_feed(lam, "feed", "owner-all-sectors")["ok"]


def test_session_and_stack_checks():
    assert not canary.check_session(_Http({}, session=504).post, "https://onssa.org")["ok"]
    http = _Http({})
    assert canary.check_session(http.post, "https://onssa.org")["ok"]
    assert http.posts[0][1]["headers"]["x-onca-session"] == "1"
    assert not canary.check_stack(_Cfn(490), "S")["ok"]


def test_operator_only_rollups_leaking_into_a_tenant_feed_fail_the_canary():
    lam = _Lam({"app-store-review": 3})
    real = lam.invoke

    def leaky(FunctionName, Payload, LogType=None):
        r = real(FunctionName, Payload, LogType)
        out = json.loads(r["Payload"].read())
        body = json.loads(gzip.decompress(base64.b64decode(out["body"])))
        body["tenant_usage"] = {"rows": [1]}
        out["body"] = base64.b64encode(gzip.compress(json.dumps(body).encode())).decode()
        r["Payload"] = io.BytesIO(json.dumps(out).encode())
        return r
    lam.invoke = leaky
    c = canary.check_feed(lam, "feed", "app-store-review")
    assert not c["ok"] and c["leaked"] == ["tenant_usage"]


def test_the_synthetic_google_user_can_never_map_to_a_tenant():
    attrs = canary.PRETOKEN_EVENT["request"]["userAttributes"]
    assert attrs["email"].endswith(".invalid")              # RFC 2606: never a real mailbox
    assert attrs["cognito:user_status"] == "EXTERNAL_PROVIDER"


def test_canary_never_raises_on_a_broken_dependency():
    class Boom:
        def invoke(self, **kw):
            raise RuntimeError("down")

        def get(self, *a, **kw):
            raise TimeoutError("t")

        post = get

        def get_paginator(self, n):
            raise RuntimeError("x")
    checks = canary.run(ENV, Boom(), Boom(), Boom())
    assert checks and not any(c["ok"] for c in checks)


# ---- size watch ---------------------------------------------------------------------------

def test_feed_growth_metric():
    m = {x["MetricName"]: x["Value"] for x in feed_builder.feed_size_metrics(7_170_171, 5_400_000)}
    assert m["FeedJsonBytes"] == 7_170_171 and m["FeedGrowthPct"] == 32.78
    assert [x["MetricName"] for x in feed_builder.feed_size_metrics(10, None)] == ["FeedJsonBytes"]


def test_feed_api_emits_an_embedded_size_metric(capsys):
    feed_api.emit_size_metric(5_800_000, 1_343_490)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["FeedResponseBytes"] == 1_343_490 and line["Service"] == "FeedApi"
    spec = line["_aws"]["CloudWatchMetrics"][0]
    assert spec["Namespace"] == "Onca" and spec["Dimensions"] == [["Service"]]


# ---- infra: what is watched, and the limits guarded --------------------------------------

def test_every_user_path_lambda_is_in_an_error_alarm():
    for fn in ("pretoken_fn", "session_fn", "oauth_fn", "self_register_fn", "feed_api_fn", "agent_fn",
               "agent_api_fn", "act_fn", "push_api_fn", "mcp_fn", "ibis_fn"):
        assert re.search(rf'"\w+": {fn}\b', MON), fn


def test_math_alarms_stay_within_cloudwatchs_10_metric_limit():
    groups = re.findall(r"_errs\(\{([^}]*)\},\s*\(([^)]*)\)\)", MON)
    assert len(groups) == 2
    for group in groups:
        assert len(re.findall(r'"\w+":', group[0])) + len(re.findall(r'"\w+"', group[1])) <= 10


def test_canary_runs_every_4_hours_and_its_alarm_covers_a_missed_run():
    assert "schedule=events.Schedule.rate(Duration.hours(4))" in MON
    block = MON[MON.index('"OncaAlarmCanary"'):]
    assert "IF(FILL(runs,0) < 1, 1, FILL(failed,0))" in block and "TreatMissingData.BREACHING" in block
    assert "period=Duration.hours(6)" in block                  # > 4 h: every window holds a run


def test_sign_in_lambdas_have_the_cpu_for_cognitos_budget():
    """2026-11-03: a 128 MB pre-token trigger took ~7.5 s cold, past Cognito's 5 s."""
    pre = APP[APP.index('"OncaPreTokenGenFn"'):APP.index('"OncaPreTokenGenFn"') + 700]
    assert int(re.search(r"memory_size=(\d+)", pre).group(1)) >= 512


def test_alerts_carry_the_alarm_description():
    from src.dashboard import alert_notifier
    h = alert_notifier._headline({"AlarmName": "Onca-Canary", "NewStateValue": "ALARM",
                                  "AlarmDescription": "ADR 030: canary failed", "NewStateReason": "1 >= 1"})
    assert "ADR 030: canary failed" in h and h.startswith("🚨")


@__import__("pytest").mark.network
def test_the_real_feed_for_the_widest_tenant_fits_the_payload_budget():
    """Scopes TODAY's published feed.json for an all-modules tenant through the real handler and
    checks the Lambda payload against the canary's budget. Skips without AWS access."""
    import pytest
    try:
        import boto3
        fn = boto3.client("lambda", region_name="us-east-1").get_function_configuration(
            FunctionName="OncaPrototypeStack-OncaFeedApi086A4381-BG0SDNWBQ32T")
        bucket = fn["Environment"]["Variables"]["ONCA_SITE_BUCKET"]
        raw = boto3.client("s3", region_name="us-east-1").get_object(Bucket=bucket, Key="feed.json")["Body"].read()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no AWS access: {type(exc).__name__}")
    from src.dashboard.feed_builder import scope_feed_to_modules
    feed = json.loads(raw)
    modules = sorted({i for a in (feed.get("entity_attrs") or {}).values() for i in a.get("industries") or []})
    scoped = scope_feed_to_modules(feed, modules)
    out = feed_api._compress(feed_api._resp(200, scoped), {"headers": {}})
    payload = len(json.dumps(out).encode())
    assert payload < canary.FEED_PAYLOAD_BUDGET, payload


def test_alerts_have_an_email_channel():
    """2026-11-03: the notifier reported teams/slack/email all None — alarms since #109 were
    delivered nowhere. The alert Lambda now carries a sender on a verified domain and the
    stack's alertEmail as recipient."""
    block = APP[APP.index('"OncaAlertNotifier"'):APP.index('"OncaAlertNotifier"') + 1200]
    assert '"ONCA_ALERT_EMAIL_FROM": "alertas@onssa.org"' in block
    assert '"ONCA_ALERT_EMAIL_TO": self.node.try_get_context("alertEmail")' in block
    import json as _j
    assert _j.loads((ROOT / "infra" / "cdk.json").read_text())["context"].get("alertEmail")
