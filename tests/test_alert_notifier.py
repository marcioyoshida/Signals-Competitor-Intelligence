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


# 2026-11-16: board alarms arrived titled "Briefing semanal · Estratégia — Onça", with the
# digest's "Movimentos: 0 · …" line, and three OK mails from new alarms' first evaluation.

def test_alert_email_has_its_own_title_and_no_digest_metrics():
    from src.dashboard import weekly_digest
    seen = []
    report = weekly_digest.send_alert("🚨 X — ALARM: boom", title="🚨 Alarme: X",
                                      email_sender=lambda *a: seen.append(a),
                                      teams_poster=lambda *a, **k: None, slack_poster=lambda *a, **k: None)
    assert report is not None
    subject, text, html = weekly_digest.format_email(
        {"title": "🚨 Alarme: X", "headline": "🚨 X — ALARM: boom", "lines": []})
    assert subject == "🚨 Alarme: X — Onça" and "Briefing semanal" not in subject + text + html
    assert "Movimentos" not in text and "Top prioridades" not in text
    default = weekly_digest.format_email({"title": "Alerta operacional", "headline": "h", "lines": []})[0]
    assert "Briefing" not in default


def test_titles_name_the_state_and_alarm():
    t = alert_notifier._title
    assert t({"AlarmName": "Onca-BoardRunFailures", "NewStateValue": "ALARM"}) == "🚨 Alarme: Onca-BoardRunFailures"
    assert t({"AlarmName": "Onca-Canary", "NewStateValue": "OK"}) == "✅ Normalizado: Onca-Canary"


def test_recovery_text_does_not_read_like_a_new_problem():
    h = alert_notifier._headline({"AlarmName": "Onca-BoardRunDuration", "NewStateValue": "OK",
                                  "AlarmDescription": "A board brief task ran over 12 min.",
                                  "NewStateReason": "r"})
    assert "voltou ao normal" in h and "Condição monitorada: A board brief task ran over 12 min." in h


def test_first_evaluation_ok_is_not_sent_but_a_real_recovery_is(monkeypatch):
    sent = []
    monkeypatch.setattr(alert_notifier.weekly_digest, "send_alert",
                        lambda headline, **kw: sent.append((headline, kw.get("title"))) or {"email": True})
    event = _sns_event(
        {"AlarmName": "Onca-New", "NewStateValue": "OK", "OldStateValue": "INSUFFICIENT_DATA"},
        {"AlarmName": "Onca-Fixed", "NewStateValue": "OK", "OldStateValue": "ALARM"},
        {"AlarmName": "Onca-Broke", "NewStateValue": "ALARM", "OldStateValue": "INSUFFICIENT_DATA"},
        {"AlarmName": "OncaCoverageAlarm-x", "NewStateValue": "ALARM"})     # synthetic, no OldState
    body = json.loads(alert_notifier.lambda_handler(event, None)["body"])
    assert [t for _, t in sent] == ["✅ Normalizado: Onca-Fixed", "🚨 Alarme: Onca-Broke",
                                    "🚨 Alarme: OncaCoverageAlarm-x"]
    assert body["sent"][0] == {"alarm": "Onca-New", "state": "OK", "skipped": "first evaluation"}
