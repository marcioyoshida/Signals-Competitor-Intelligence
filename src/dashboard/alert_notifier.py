"""Operational alerting (issue #109) — turns a CloudWatch Alarm state change into a push
notification on the SAME Teams/Slack/email channels as the weekly CSO digest
(`src/dashboard/weekly_digest.py`), so there is one delivery config, not two.

Two alarms feed this (wired in `infra/app.py`):
  - `OncaPipeline` `ExecutionsFailed` / `ExecutionsTimedOut` — the daily pipeline broke.
  - the `Onca/FeedPublished` custom metric (emitted by `feed_builder.lambda_handler` on every
    successful publish) going quiet — `feed.json` has gone stale even though nothing "failed".

Both alarms target one SNS topic; this Lambda is its only subscriber. CloudWatch Alarm SNS
messages are JSON with `AlarmName` / `NewStateValue` / `NewStateReason` — nothing here is
inferred beyond what CloudWatch reported, so ALARM and OK (recovery) are both surfaced
honestly instead of only ever crying wolf.
"""
from __future__ import annotations

import json
from typing import Any

from src.dashboard import weekly_digest

_ICON = {"ALARM": "🚨", "OK": "✅", "INSUFFICIENT_DATA": "⚠️"}
_STATE_PT = {"ALARM": "Alarme", "OK": "Normalizado", "INSUFFICIENT_DATA": "Sem dados"}


def _headline(msg: dict[str, Any]) -> str:
    state = str(msg.get("NewStateValue") or "")
    icon = _ICON.get(state, "⚠️")
    name = msg.get("AlarmName") or "Onca alarm"
    reason = msg.get("NewStateReason") or ""
    # ADR 030 alarms carry a description saying what broke and where to look. On recovery the
    # description is the condition that WAS watched, so it is labelled as such: an OK mail must
    # not read like a new problem.
    what = msg.get("AlarmDescription") or ""
    if state == "OK" and what:
        return f"{icon} {name} — OK: voltou ao normal. Condição monitorada: {what} {reason}".rstrip()
    return f"{icon} {name} — {state}: {what + ' ' if what else ''}{reason}"


def _title(msg: dict[str, Any]) -> str:
    """The e-mail subject / message title: an operational alert, never the weekly briefing's."""
    state = str(msg.get("NewStateValue") or "")
    return (f"{_ICON.get(state, '⚠️')} {_STATE_PT.get(state, state or 'Alerta')}: "
            f"{msg.get('AlarmName') or 'alarme Onça'}")


def _is_noise(msg: dict[str, Any]) -> bool:
    """A new or idle alarm's first evaluation (INSUFFICIENT_DATA → OK) recovered nothing. Only a
    real recovery (ALARM → OK) is sent. Synthetic messages without OldStateValue still go out."""
    return msg.get("NewStateValue") == "OK" and msg.get("OldStateValue") == "INSUFFICIENT_DATA"


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    import os

    dashboard_url = os.environ.get("ONCA_DASHBOARD_URL")
    sent: list[dict[str, Any]] = []
    for record in event.get("Records") or []:
        sns = record.get("Sns") or {}
        try:
            msg = json.loads(sns.get("Message") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(msg, dict) or not msg.get("AlarmName"):
            continue
        if _is_noise(msg):
            sent.append({"alarm": msg.get("AlarmName"), "state": "OK", "skipped": "first evaluation"})
            continue
        report = weekly_digest.send_alert(_headline(msg), title=_title(msg), dashboard_url=dashboard_url)
        sent.append({"alarm": msg.get("AlarmName"), "state": msg.get("NewStateValue"),
                     "report": report})
    # the delivery report goes to the log too: an SNS-invoked Lambda's return value is discarded
    print(json.dumps({"alert_delivery": sent}, ensure_ascii=False, default=str))
    return {"statusCode": 200, "body": json.dumps({"sent": sent}, ensure_ascii=False, default=str)}
