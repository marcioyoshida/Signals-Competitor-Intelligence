"""Agentic API token-usage counters — the metering input for Storefront's
Stripe usage-reporting job (ADR: storefront/docs/adr-agentic-api-billing.md).

Table `onca-api-usage`: PK `tenant_id`, SK `period` (`YYYY-MM`, UTC). One row
per tenant per billing cycle; `tokens_used`/`calls` are atomic DynamoDB ADD
counters so concurrent API calls never race-lose an increment.
"""
from __future__ import annotations

import datetime
import os
from typing import Any


def _table(table: Any | None = None) -> Any:
    if table is not None:
        return table
    import boto3

    return boto3.resource("dynamodb").Table(
        os.environ.get("ONCA_API_USAGE_TABLE", "onca-api-usage")
    )


def _period(when: datetime.datetime | None = None) -> str:
    when = when or datetime.datetime.now(datetime.timezone.utc)
    return when.strftime("%Y-%m")


def record_usage(tenant_id: str, tokens: int, *, table: Any | None = None) -> None:
    """Best-effort — never let a metering write break the caller's response."""
    if tokens <= 0:
        return
    try:
        _table(table).update_item(
            Key={"tenant_id": str(tenant_id), "period": _period()},
            UpdateExpression="ADD tokens_used :t, calls :c SET updated_at = :u",
            ExpressionAttributeValues={
                ":t": int(tokens), ":c": 1,
                ":u": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
        )
    except Exception as exc:  # pragma: no cover
        print(f"Warning: record_usage failed: {exc}")


def get_usage(tenant_id: str, period: str | None = None, *, table: Any | None = None) -> dict[str, Any]:
    """Current-cycle usage for the admin panel's usage display. Zeros, not an
    error, when nothing has been recorded yet."""
    period = period or _period()
    try:
        item = _table(table).get_item(
            Key={"tenant_id": str(tenant_id), "period": period}
        ).get("Item")
    except Exception as exc:  # pragma: no cover
        print(f"Warning: get_usage failed: {exc}")
        item = None
    return {
        "period": period,
        "tokens_used": int((item or {}).get("tokens_used") or 0),
        "calls": int((item or {}).get("calls") or 0),
    }
