"""OncaBoardRuns (#322): one item per run, one per officer response per phase. Tenant-scoped.

Keys: ``tenant`` (partition) + ``sk``:
- ``RUN#<run_id>`` — the run: status, task, target, pack sha, cost, timings;
- ``RUN#<run_id>#R#<phase>#<officer>`` — one officer response (append-only: written once).

A run id is derived from the tenant and the request's idempotency key, so a retried
``board_brief`` act resolves to the same run instead of paying for a second one.
Items carry ``expires_at`` (epoch seconds, the table's TTL attribute): 90 days, the privacy
policy's log retention, unless a tenant contract sets another value (ADR 031 open question).
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

RETENTION_DAYS = 90
_NS = uuid.UUID("6f1c2b0e-4d1a-4c55-9a51-0b6a0c0b0a31")  # fixed: run ids must stay stable

STATUSES = ("queued", "running", "complete", "partial", "failed", "refused")
TERMINAL = frozenset({"complete", "partial", "failed", "refused"})


def run_id_for(tenant: str, idempotency_key: str) -> str:
    if not tenant or not idempotency_key:
        raise ValueError("tenant and idempotency_key are required")
    return str(uuid.uuid5(_NS, f"{tenant}\x00{idempotency_key}"))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _expires(now: dt.datetime, days: int = RETENTION_DAYS) -> int:
    return int((now + dt.timedelta(days=days)).timestamp())


def _ddb(v: Any) -> Any:
    """JSON-ish → DynamoDB-safe (floats as Decimal, empty strings kept)."""
    return json.loads(json.dumps(v, ensure_ascii=False), parse_float=Decimal)


def _code(exc: Exception) -> str | None:
    return (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")


def create_run(table: Any, *, tenant: str, run_id: str, task: str, target: str,
               requested_by: str | None = None, now: dt.datetime | None = None) -> tuple[dict[str, Any], bool]:
    """Create the run item once. Returns (item, created); an existing run is returned as is."""
    now = now or _now()
    item = {"tenant": tenant, "sk": f"RUN#{run_id}", "run_id": run_id, "status": "queued",
            "task": task, "target": target, "requested_by": requested_by,
            "created_at": now.isoformat(timespec="seconds"), "expires_at": _expires(now)}
    try:
        table.put_item(Item=_ddb(item), ConditionExpression="attribute_not_exists(sk)")
        return item, True
    except Exception as exc:
        if _code(exc) != "ConditionalCheckFailedException":
            raise
    return get_run(table, tenant, run_id) or item, False


def get_run(table: Any, tenant: str, run_id: str) -> dict[str, Any] | None:
    return table.get_item(Key={"tenant": tenant, "sk": f"RUN#{run_id}"}).get("Item")


def set_status(table: Any, *, tenant: str, run_id: str, status: str, **fields: Any) -> None:
    """Move a run forward. A terminal run never changes again."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    names, values, sets = {"#s": "status"}, {":s": status}, ["#s = :s"]
    for i, (k, v) in enumerate(sorted(fields.items())):
        names[f"#f{i}"] = k
        values[f":f{i}"] = _ddb(v)
        sets.append(f"#f{i} = :f{i}")
    terminal = sorted(TERMINAL)
    values.update({f":t{i}": t for i, t in enumerate(terminal)})
    try:
        table.update_item(
            Key={"tenant": tenant, "sk": f"RUN#{run_id}"},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_exists(sk) AND NOT (#s IN ("
                                + ", ".join(f":t{i}" for i in range(len(terminal))) + "))",
            ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    except Exception as exc:
        if _code(exc) == "ConditionalCheckFailedException":
            raise ValueError(f"run {run_id} is missing or already finished") from exc
        raise


def put_response(table: Any, *, tenant: str, run_id: str, phase: str, officer: str,
                 response: dict[str, Any], now: dt.datetime | None = None) -> bool:
    """Write one officer response once (append-only). Returns False if it was already there."""
    now = now or _now()
    item = {"tenant": tenant, "sk": f"RUN#{run_id}#R#{phase}#{officer}", "run_id": run_id,
            "phase": phase, "officer": officer, "status": response.get("status"),
            "response": _ddb(response), "at": now.isoformat(timespec="seconds"),
            "expires_at": _expires(now)}
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(sk)")
        return True
    except Exception as exc:
        if _code(exc) == "ConditionalCheckFailedException":
            return False
        raise


def record_result(table: Any, *, tenant: str, result: dict[str, Any]) -> int:
    """Persist every officer response of a runner result. Returns how many were new."""
    n = 0
    for phase, by_officer in (result.get("phases") or {}).items():
        for officer, resp in by_officer.items():
            n += put_response(table, tenant=tenant, run_id=result["run_id"], phase=phase,
                              officer=officer, response=resp)
    return n


def list_runs(table: Any, tenant: str, limit: int = 20) -> list[dict[str, Any]]:
    """The tenant's runs (not their responses), newest first. Never crosses tenants."""
    from boto3.dynamodb.conditions import Key

    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"KeyConditionExpression": Key("tenant").eq(tenant) & Key("sk").begins_with("RUN#")}
    while True:
        page = table.query(**kwargs)
        items += [i for i in page.get("Items", []) if i["sk"].count("#") == 1]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return sorted(items, key=lambda i: i.get("created_at") or "", reverse=True)[:limit]


def decision_key(run_id: str) -> str:
    return f"RUN#{run_id}#DECISION"


def get_decision(table: Any, tenant: str, run_id: str) -> dict[str, Any] | None:
    return table.get_item(Key={"tenant": tenant, "sk": decision_key(run_id)}).get("Item")


def link_decision(table: Any, *, tenant: str, run_id: str, decision: dict[str, Any],
                  now: dt.datetime | None = None) -> bool:
    """#327 human gate: one decision per run, written once. False if the run already has one."""
    now = now or _now()
    item = {"tenant": tenant, "sk": decision_key(run_id), "run_id": run_id,
            **_ddb(decision), "at": now.isoformat(timespec="seconds"), "expires_at": _expires(now)}
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(sk)")
        return True
    except Exception as exc:
        if _code(exc) == "ConditionalCheckFailedException":
            return False
        raise


EVAL_TENANT = "_eval"          # #340: harness scores live apart from every customer tenant
EVAL_RETENTION_DAYS = 365      # public data and synthetic packs only, nothing of a customer


def eval_key(run_id: str, gate_version: Any) -> str:
    return f"RUN#{run_id}#EVAL#v{gate_version}"


def put_eval(table: Any, *, run_id: str, scores: dict[str, Any], tenant: str = EVAL_TENANT,
             now: dt.datetime | None = None) -> bool:
    """#340: one score per run and gate version, written once. Also creates the run item for
    a dry run scored from a file, so ``list_runs(_eval)`` lists it. False if already scored."""
    now = now or _now()
    g = scores.get("gate") or {}
    if tenant == EVAL_TENANT:
        run = {"tenant": tenant, "sk": f"RUN#{run_id}", "run_id": run_id, "status": scores.get("status"),
               "task": scores.get("scenario"), "target": scores.get("scenario"), "model": scores.get("model"),
               "provider": scores.get("provider"), "pack_sha": scores.get("pack_sha256"),
               "created_at": now.isoformat(timespec="seconds"),
               "expires_at": _expires(now, EVAL_RETENTION_DAYS)}
        try:
            table.put_item(Item=_ddb(run), ConditionExpression="attribute_not_exists(sk)")
        except Exception as exc:
            if _code(exc) != "ConditionalCheckFailedException":
                raise
    item = {"tenant": tenant, "sk": eval_key(run_id, g.get("version")), "run_id": run_id,
            "scenario": scores.get("scenario"), "passed": bool(g.get("passed")),
            "scores": _ddb(scores), "at": now.isoformat(timespec="seconds"),
            "expires_at": _expires(now, EVAL_RETENTION_DAYS)}
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(sk)")
        return True
    except Exception as exc:
        if _code(exc) == "ConditionalCheckFailedException":
            return False
        raise
