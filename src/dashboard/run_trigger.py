"""Ad-hoc "run soon" trigger — schedules ONE pipeline run at the top of the next
hour, debounced so repeated clicks never stack runs.

Fronted by CloudFront like the other operator endpoints (basic-auth edge + a
shared origin secret; the Function URL itself is AuthType NONE). The debounce is
structural: a single fixed-name EventBridge Scheduler one-shot is created-or-
updated, so N clicks converge on exactly one schedule → one run. The schedule
targets the OncaPipeline state machine and self-deletes after firing
(ActionAfterCompletion=DELETE). "Next hour" (not "now") aligns with the batch
cadence; off-cycle runs are cheap — diff dedup + emit-on-change surface only
genuinely-new signals.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

# Brazil is UTC-3 year-round (no DST since 2019), so a fixed offset gives the
# correct BRT wall-clock; the Scheduler also gets ScheduleExpressionTimezone.
_BRT = dt.timezone(dt.timedelta(hours=-3))


def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def next_top_of_hour(now: dt.datetime | None = None) -> str:
    """Wall-clock string (BRT) for the top of the next hour: 'YYYY-MM-DDTHH:00:00'."""
    now = now or dt.datetime.now(_BRT)
    nxt = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    return nxt.strftime("%Y-%m-%dT%H:%M:%S")


def _method(event: dict[str, Any]) -> str:
    ctx = (event.get("requestContext") or {}).get("http") or {}
    return str(ctx.get("method") or "POST").upper()


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    secret = os.environ.get("ONCA_ORIGIN_SECRET")
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    if secret and headers.get("x-onca-origin") != secret:
        return _resp(403, {"error": "forbidden"})

    name = os.environ.get("ONCA_SCHEDULE_NAME", "onca-adhoc-run")
    import boto3

    sch = boto3.client("scheduler")

    if _method(event) == "GET":
        try:
            g = sch.get_schedule(Name=name)
            return _resp(200, {
                "pending": True,
                "at": g.get("ScheduleExpression"),
                "timezone": g.get("ScheduleExpressionTimezone"),
            })
        except sch.exceptions.ResourceNotFoundException:
            return _resp(200, {"pending": False})

    pipeline_arn = os.environ.get("ONCA_PIPELINE_ARN")
    role_arn = os.environ.get("ONCA_SCHEDULER_ROLE_ARN")
    if not pipeline_arn or not role_arn:
        return _resp(500, {"error": "trigger not configured"})

    at = next_top_of_hour()
    params: dict[str, Any] = {
        "Name": name,
        "ScheduleExpression": f"at({at})",
        "ScheduleExpressionTimezone": "America/Sao_Paulo",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {"Arn": pipeline_arn, "RoleArn": role_arn, "Input": "{}"},
        "ActionAfterCompletion": "DELETE",
        "State": "ENABLED",
        "Description": "Onca ad-hoc pipeline run (debounced, self-deleting).",
    }
    try:
        sch.create_schedule(**params)
        created = True
    except sch.exceptions.ConflictException:
        # A run is already pending — overwrite the same schedule (debounce). N
        # clicks in the same hour converge on one run at the same target time.
        sch.update_schedule(**params)
        created = False

    return _resp(200, {
        "status": "scheduled",
        "at": at,
        "timezone": "America/Sao_Paulo",
        "created": created,
        "debounced": not created,
    })
