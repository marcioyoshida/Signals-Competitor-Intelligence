"""SNS -> Teams/Slack/email operational alerting (issue #109)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import alert_notifier


def _sns_event(*messages: dict) -> dict:
    return {"Records": [{"Sns": {"Message": json.dumps(m)}} for m in messages]}


def test_headline_uses_icon_for_alarm_state():
    h = alert_notifier._headline({
        "AlarmName": "OncaPipelineFailedAlarm", "NewStateValue": "ALARM",
        "NewStateReason": "Threshold Crossed: 1 datapoint [1.0] was >= 1.0",
    })
    assert h.startswith("🚨 OncaPipelineFailedAlarm")
    assert "Threshold Crossed" in h


def test_headline_uses_icon_for_ok_state():
    h = alert_notifier._headline({
        "AlarmName": "OncaFeedStaleAlarm", "NewStateValue": "OK",
        "NewStateReason": "Threshold Crossed: no longer breaching",
    })
    assert h.startswith("✅ OncaFeedStaleAlarm")


def test_lambda_handler_sends_one_alert_per_record(monkeypatch):
    sent = []
    monkeypatch.setattr(
        alert_notifier.weekly_digest, "send_alert",
        lambda headline, **kw: sent.append(headline) or {"teams": True, "slack": None, "email": None},
    )
    event = _sns_event(
        {"AlarmName": "OncaPipelineFailedAlarm", "NewStateValue": "ALARM", "NewStateReason": "r1"},
        {"AlarmName": "OncaFeedStaleAlarm", "NewStateValue": "OK", "NewStateReason": "r2"},
    )
    resp = alert_notifier.lambda_handler(event, None)
    assert resp["statusCode"] == 200
    assert len(sent) == 2
    assert "OncaPipelineFailedAlarm" in sent[0]
    body = json.loads(resp["body"])
    assert len(body["sent"]) == 2


def test_lambda_handler_ignores_malformed_or_non_alarm_records(monkeypatch):
    monkeypatch.setattr(alert_notifier.weekly_digest, "send_alert",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not send")))
    event = {"Records": [
        {"Sns": {"Message": "not json"}},
        {"Sns": {"Message": json.dumps({"NoAlarmName": True})}},
    ]}
    resp = alert_notifier.lambda_handler(event, None)
    assert json.loads(resp["body"])["sent"] == []
