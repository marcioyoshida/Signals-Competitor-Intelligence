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


def _headline(msg: dict[str, Any]) -> str:
    state = str(msg.get("NewStateValue") or "")
    icon = _ICON.get(state, "⚠️")
    name = msg.get("AlarmName") or "Onca alarm"
    reason = msg.get("NewStateReason") or ""
    return f"{icon} {name} — {state}: {reason}"


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
        report = weekly_digest.send_alert(_headline(msg), dashboard_url=dashboard_url)
        sent.append({"alarm": msg.get("AlarmName"), "state": msg.get("NewStateValue"),
                     "report": report})
    return {"statusCode": 200, "body": json.dumps({"sent": sent}, ensure_ascii=False)}
