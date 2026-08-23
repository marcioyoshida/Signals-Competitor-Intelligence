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


def _current_step(sfn: Any, execution_arn: str) -> str | None:
    """Name of the most recent state the running execution entered, else None.

    Walks the execution history from the newest event and returns the `name`
    carried by the first state-entry event (Task/Pass/Wait/Choice/Map/Succeed/
    Fail all surface a `*StateEnteredEventDetails.name`). Guaranteed once the
    engine has dispatched the first state; None only while it is initializing.
    """
    try:
        hist = sfn.get_execution_history(
            executionArn=execution_arn, reverseOrder=True, maxResults=50
        )
    except Exception:
        return None
    for ev in hist.get("events") or []:
        for key, details in (ev.get("eventDetails") or {}).items():
            if key.endswith("StateEnteredEventDetails") and isinstance(details, dict):
                return details.get("name")
    return None


def _latest_execution(sfn: Any, pipeline_arn: str) -> dict[str, Any] | None:
    """Latest pipeline run as {status, started_at, current_step?}, or None.

    status mirrors Execution::Status (RUNNING/SUCCEEDED/FAILED/ABORTED/…). The
    current step is reported only while RUNNING — a finished run's last state
    bears no progress meaning.
    """
    try:
        lst = sfn.list_executions(stateMachineArn=pipeline_arn, maxResults=1)
        executions = lst.get("executions") or []
        if not executions:
            return None
        latest = executions[0]
        desc = sfn.describe_execution(executionArn=latest["executionArn"])
        started = desc.get("startDate")
        info: dict[str, Any] = {
            "status": desc.get("status"),
            # boto returns a datetime; the JSON response must carry a string.
            "started_at": started.isoformat() if hasattr(started, "isoformat") else started,
        }
        if desc.get("status") == "RUNNING":
            step = _current_step(sfn, latest["executionArn"])
            info["current_step"] = step or "running"
        return info
    except Exception:
        return None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    secret = os.environ.get("ONCA_ORIGIN_SECRET")
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    if secret and headers.get("x-onca-origin") != secret:
        return _resp(403, {"error": "forbidden"})

    name = os.environ.get("ONCA_SCHEDULE_NAME", "onca-adhoc-run")
    pipeline_arn = os.environ.get("ONCA_PIPELINE_ARN")
    import boto3

    sch = boto3.client("scheduler")

    if _method(event) == "GET":
        try:
            g = sch.get_schedule(Name=name)
            pending: dict[str, Any] = {
                "pending": True,
                "at": g.get("ScheduleExpression"),
                "timezone": g.get("ScheduleExpressionTimezone"),
            }
        except sch.exceptions.ResourceNotFoundException:
            pending = {"pending": False}
        # Latest pipeline execution (any source), live step while running.
        execution: dict[str, Any] = {"status": "UNKNOWN"}
        if pipeline_arn:
            got = _latest_execution(boto3.client("stepfunctions"), pipeline_arn)
            if got:
                execution = got
            else:
                execution = {"status": "NONE"}
        return _resp(200, {**pending, "execution": execution})

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
