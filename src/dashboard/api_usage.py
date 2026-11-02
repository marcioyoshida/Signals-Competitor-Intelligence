"""Agentic API token-usage counters — the metering input for Storefront's
Stripe usage-reporting job (ADR: storefront/docs/adr-agentic-api-billing.md).

Table `onca-api-usage`: PK `tenant_id`, SK `period` (`YYYY-MM`, UTC). One row
per tenant per billing cycle; `tokens_used`/`calls` are atomic DynamoDB ADD
counters so concurrent API calls never race-lose an increment.

#305 / ADR 025: included quota + overage. A tenant's monthly quota is
``api_quota_tokens`` on its tenant_config row, else ``QUOTA_PER_MODULE`` x its licensed
modules (owner decision 2026-11-02: 1M tokens per module per month). Past the quota the
call is still SERVED (ADR 025 rejects a hard cutoff; the per-key rate limit stays the abuse
ceiling). The part of each call above the quota is ADDed to ``overage_tokens`` and the
month's row is flagged ``overage = true``: that is what the Storefront usage reporter bills.
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


QUOTA_PER_MODULE = 1_000_000


def quota_for(cfg: dict[str, Any] | None) -> int:
    """The tenant's included tokens per month (0 when it licenses nothing)."""
    cfg = cfg or {}
    try:
        explicit = int(cfg.get("api_quota_tokens") or 0)
    except (TypeError, ValueError):
        explicit = 0
    return explicit if explicit > 0 else QUOTA_PER_MODULE * len(cfg.get("modules") or [])


def overage_slice(used_after: int, tokens: int, quota: int) -> int:
    """How much of THIS call's ``tokens`` fell above the quota, given the atomic post-ADD total.
    Each concurrent call sees its own ``used_after``, so the slices add up exactly."""
    return max(0, min(int(tokens), int(used_after) - int(quota)))


def record_usage(tenant_id: str, tokens: int, *, quota: int | None = None,
                 table: Any | None = None) -> dict[str, Any] | None:
    """Best-effort — never let a metering write break the caller's response. Returns the
    period's ``{period, tokens_used, quota, remaining, overage_tokens}`` (None on failure or a
    zero-token call). With ``quota``, the part above it is marked as overage."""
    period = _period()
    if tokens <= 0:
        return None
    try:
        t = _table(table)
        r = t.update_item(
            Key={"tenant_id": str(tenant_id), "period": period},
            UpdateExpression="ADD tokens_used :t, calls :c SET updated_at = :u",
            ExpressionAttributeValues={
                ":t": int(tokens), ":c": 1,
                ":u": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
            ReturnValues="UPDATED_NEW",
        )
        used = int(((r or {}).get("Attributes") or {}).get("tokens_used") or 0)
        out: dict[str, Any] = {"period": period, "tokens_used": used}
        if quota is not None:
            over = overage_slice(used, tokens, quota)
            if over:
                t.update_item(
                    Key={"tenant_id": str(tenant_id), "period": period},
                    UpdateExpression="ADD overage_tokens :o SET overage = :y",
                    ExpressionAttributeValues={":o": over, ":y": True},
                )
            out.update(quota=int(quota), remaining=max(0, int(quota) - used), overage_tokens=over)
        return out
    except Exception as exc:  # pragma: no cover
        print(f"Warning: record_usage failed: {exc}")
        return None


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
        "overage_tokens": int((item or {}).get("overage_tokens") or 0),
    }


def quota_headers(quota: int, used: int, period: str) -> dict[str, str]:
    return {"X-Onca-Quota-Limit": str(int(quota)), "X-Onca-Quota-Remaining": str(max(0, int(quota) - int(used))),
            "X-Onca-Quota-Period": period}
