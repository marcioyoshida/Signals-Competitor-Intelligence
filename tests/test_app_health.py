"""#312/#318: store-app health events + minimum supported version (POST /api/session/app)."""
import json

from src.dashboard import app_health, session_api

POLICY = json.dumps({"ios": {"soft": "0.3.0", "hard": "0.2.0"}, "android": {"soft": "0.2.0"}})


def _post(body, monkeypatch, policy=POLICY, raw=None):
    monkeypatch.setenv("ONCA_APP_MIN_VERSIONS", policy)
    ev = {"rawPath": "/api/session/app", "headers": {"x-onca-session": "1"},
          "body": raw if raw is not None else json.dumps(body)}
    r = session_api.lambda_handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


def test_version_verdicts(monkeypatch):
    p = json.loads(POLICY)
    assert app_health.verdict("ios", "0.1.9", p) == "hard"
    assert app_health.verdict("ios", "0.2.5", p) == "soft"
    assert app_health.verdict("ios", "0.3.0", p) == "ok"
    assert app_health.verdict("ios", None, p) == "soft"          # unknown: never hard
    assert app_health.verdict("android", None, {"android": {"hard": "9.9.9"}}) == "ok"
    assert app_health.verdict("ios", "1.0.0", {}) == "ok"


def test_start_returns_the_verdict_and_store_and_counts(monkeypatch, capsys):
    st, out = _post({"kind": "start", "platform": "ios", "version": "0.1.0", "build": 5, "bridge": 4}, monkeypatch)
    assert st == 200 and out["update"] == "hard" and out["store"].startswith("https://apps.apple.com/")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["Kind"] == "start" and line["AppEvents"] == 1 and line["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Onca/App"


def test_probe_is_not_counted(monkeypatch, capsys):
    st, out = _post({"kind": "probe", "platform": "ios", "version": "0.0.1"}, monkeypatch)
    assert st == 200 and out["update"] == "hard" and capsys.readouterr().out == ""


def test_error_keeps_only_route_and_status(monkeypatch, capsys):
    st, _ = _post({"kind": "error", "platform": "android", "source": "api", "route": "/api/feed?token=x#y",
                   "status": 502, "message": "secret text", "email": "a@b.c", "stack": "at x"}, monkeypatch)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert st == 200 and line["route"] == "/api/feed" and line["status"] == 502
    flat = json.dumps(line)
    assert "secret text" not in flat and "a@b.c" not in flat and "token" not in flat and "stack" not in flat


def test_rejects_unknown_kinds_bad_routes_and_big_bodies(monkeypatch, capsys):
    assert _post({"kind": "boom"}, monkeypatch)[0] == 400
    _post({"kind": "error", "route": "https://evil.example/api/x", "status": 9999, "build": True}, monkeypatch)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "route" not in line and "status" not in line and "build" not in line and line["platform"] == "other"
    assert _post(None, monkeypatch, raw="x" * 3000)[0] == 413


def test_needs_the_csrf_header():
    r = session_api.lambda_handler({"rawPath": "/api/session/app", "headers": {}, "body": "{}"}, None)
    assert r["statusCode"] == 403


def test_policy_from_cdk_context_is_well_formed():
    from pathlib import Path
    ctx = json.loads((Path(__file__).resolve().parents[1] / "infra" / "cdk.json").read_text())["context"]
    pol = ctx["app_min_versions"]
    assert set(pol) == {"ios", "android"}
    for levels in pol.values():
        assert all(app_health.parse_version(v) for v in levels.values())
