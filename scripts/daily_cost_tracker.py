#!/usr/bin/env python3
"""Generic daily AWS Cost Explorer tracker.

Parameters are inputs, not project-specific constants:

    python scripts/daily_cost_tracker.py \\
        --service bedrock \\
        --tag tr:project-name \\
        --profile my2027

`--service bedrock` expands to every Cost Explorer SERVICE whose name
contains "Bedrock" (the Bedrock product plus Agent / AgentCore / future
siblings). Model spend is not a separate SERVICE — it lands as USAGE_TYPE
under those services (NovaLite, TitanEmbeddingV2, …), so the report always
splits by usage type as well as by the given cost-allocation tag.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

SERVICE_PRESETS: dict[str, re.Pattern[str]] = {
    # Product + every model-bearing sibling service CE may emit.
    "bedrock": re.compile(r"bedrock", re.IGNORECASE),
}

UNTAGGED = "(untagged)"


def normalize_tag_value(value: str | None, tag_key: str | None) -> str:
    """CE emits `<tagKey>$` (and empty) for resources with no value on that key."""
    if not value or value in ("$", "NoTagKey", UNTAGGED):
        return UNTAGGED
    if tag_key and value in (f"{tag_key}$", f"{tag_key}$unallocated"):
        return UNTAGGED
    return value


@dataclass
class CostRow:
    start: str
    keys: dict[str, str]
    cost: float
    quantity: float
    estimated: bool = False


@dataclass
class TrackerReport:
    account: str | None
    start: str
    end: str
    services: list[str]
    tag_key: str
    tag_values: list[str]
    rows: list[CostRow]
    warnings: list[str] = field(default_factory=list)
    tag_grouping: bool = True

    def totals_by_tag(self) -> list[tuple[str, float]]:
        acc: dict[str, float] = {}
        for row in self.rows:
            acc[row.keys.get("tag") or UNTAGGED] = (
                acc.get(row.keys.get("tag") or UNTAGGED, 0.0) + row.cost
            )
        return sorted(acc.items(), key=lambda kv: (-kv[1], kv[0]))

    def totals_by_usage_type(self) -> list[tuple[str, float]]:
        acc: dict[str, float] = {}
        for row in self.rows:
            key = row.keys.get("usage_type") or "(none)"
            acc[key] = acc.get(key, 0.0) + row.cost
        return sorted(acc.items(), key=lambda kv: (-kv[1], kv[0]))

    def daily_totals(self) -> list[tuple[str, float]]:
        acc: dict[str, float] = {}
        for row in self.rows:
            acc[row.start] = acc.get(row.start, 0.0) + row.cost
        return sorted(acc.items())

    def grand_total(self) -> float:
        return sum(r.cost for r in self.rows)


def resolve_services(
    requested: Iterable[str], available: Iterable[str]
) -> list[str]:
    """Map CLI --service values (presets or names) onto CE SERVICE values."""
    available_list = list(available)
    by_lower = {s.lower(): s for s in available_list}
    out: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name not in seen:
            seen.add(name)
            out.append(name)

    for raw in requested:
        token = raw.strip()
        if not token:
            continue
        preset = SERVICE_PRESETS.get(token.lower())
        if preset is not None:
            matched = [s for s in available_list if preset.search(s)]
            if not matched:
                raise ValueError(
                    f"preset {token!r} matched no Cost Explorer services "
                    f"in this period (available={available_list[:12]})"
                )
            for s in matched:
                add(s)
            continue
        exact = by_lower.get(token.lower())
        if exact:
            add(exact)
            continue
        partial = [s for s in available_list if token.lower() in s.lower()]
        if not partial:
            raise ValueError(
                f"service {token!r} not found in Cost Explorer. "
                f"Use a preset ({', '.join(SERVICE_PRESETS)}) or a CE SERVICE name."
            )
        for s in partial:
            add(s)
    return out


def usage_filter(
    services: list[str],
    *,
    tag_key: str | None = None,
    tag_values: list[str] | None = None,
    usage_only: bool = True,
) -> dict[str, Any]:
    """Build a GetCostAndUsage Filter (credits excluded by default)."""
    parts: list[dict[str, Any]] = []
    if services:
        parts.append({"Dimensions": {"Key": "SERVICE", "Values": services}})
    if usage_only:
        parts.append({"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}})
    if tag_key and tag_values:
        parts.append({"Tags": {"Key": tag_key, "Values": tag_values}})
    if not parts:
        return {}
    if len(parts) == 1:
        return parts[0]
    return {"And": parts}


def group_by_specs(tag_key: str | None, *, include_tag: bool) -> list[dict[str, str]]:
    groups: list[dict[str, str]] = []
    if include_tag and tag_key:
        groups.append({"Type": "TAG", "Key": tag_key})
    groups.append({"Type": "DIMENSION", "Key": "USAGE_TYPE"})
    return groups


def flatten_results(
    payload: dict[str, Any],
    *,
    tag_key: str | None,
    include_tag: bool,
) -> list[CostRow]:
    rows: list[CostRow] = []
    group_defs = payload.get("GroupDefinitions") or []
    for day in payload.get("ResultsByTime") or []:
        start = (day.get("TimePeriod") or {}).get("Start") or ""
        estimated = bool(day.get("Estimated"))
        groups = day.get("Groups") or []
        if not groups:
            total = day.get("Total") or {}
            cost = float((total.get("UnblendedCost") or {}).get("Amount") or 0)
            qty = float((total.get("UsageQuantity") or {}).get("Amount") or 0)
            if cost or qty:
                rows.append(
                    CostRow(
                        start=start,
                        keys={"tag": UNTAGGED, "usage_type": "(none)"},
                        cost=cost,
                        quantity=qty,
                        estimated=estimated,
                    )
                )
            continue
        for group in groups:
            keys = list(group.get("Keys") or [])
            parsed: dict[str, str] = {}
            for spec, value in zip(group_defs, keys):
                gtype = spec.get("Type")
                if gtype == "TAG":
                    parsed["tag"] = normalize_tag_value(value, tag_key)
                elif spec.get("Key") == "USAGE_TYPE":
                    parsed["usage_type"] = value or "(none)"
                elif spec.get("Key") == "SERVICE":
                    parsed["service"] = value
            if include_tag and "tag" not in parsed:
                parsed["tag"] = UNTAGGED
            metrics = group.get("Metrics") or {}
            cost = float((metrics.get("UnblendedCost") or {}).get("Amount") or 0)
            qty = float((metrics.get("UsageQuantity") or {}).get("Amount") or 0)
            if cost == 0 and qty == 0:
                continue
            rows.append(
                CostRow(
                    start=start,
                    keys=parsed,
                    cost=cost,
                    quantity=qty,
                    estimated=estimated,
                )
            )
    return rows


def period_for_days(days: int, *, today: date | None = None) -> tuple[str, str]:
    """Inclusive lookback of `days` ending today. CE End is exclusive."""
    if days < 1:
        raise ValueError("days must be >= 1")
    end = today or date.today()
    start = end - timedelta(days=days - 1)
    return start.isoformat(), (end + timedelta(days=1)).isoformat()


def _paginate_cost_and_usage(ce: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] | None = None
    token: str | None = None
    while True:
        call = dict(kwargs)
        if token:
            call["NextPageToken"] = token
        page = ce.get_cost_and_usage(**call)
        if merged is None:
            merged = page
        else:
            merged.setdefault("ResultsByTime", []).extend(page.get("ResultsByTime") or [])
        token = page.get("NextPageToken")
        if not token:
            break
    return merged or {"ResultsByTime": []}


def list_services(ce: Any, start: str, end: str) -> list[str]:
    names: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "TimePeriod": {"Start": start, "End": end},
            "Dimension": "SERVICE",
        }
        if token:
            kwargs["NextPageToken"] = token
        page = ce.get_dimension_values(**kwargs)
        names.extend(v["Value"] for v in page.get("DimensionValues") or [])
        token = page.get("NextPageToken")
        if not token:
            break
    return names


def _is_tag_grouping_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        needle in msg
        for needle in (
            "group definition tag",
            "invalid tag",
            "tag is invalid",
            "cost allocation tag",
            "not authorized to use tag",
        )
    )


def query_daily_costs(
    ce: Any,
    *,
    start: str,
    end: str,
    services: list[str],
    tag_key: str,
    tag_values: list[str] | None = None,
    usage_only: bool = True,
    account: str | None = None,
) -> TrackerReport:
    """Daily UnblendedCost grouped by tag + USAGE_TYPE (models).

    If the tag is not an active cost-allocation tag, falls back to
    USAGE_TYPE-only grouping and records a warning.
    """
    warnings: list[str] = []
    filt = usage_filter(
        services, tag_key=tag_key, tag_values=tag_values, usage_only=usage_only
    )
    base_kwargs: dict[str, Any] = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost", "UsageQuantity"],
    }
    if filt:
        base_kwargs["Filter"] = filt

    include_tag = True
    try:
        payload = _paginate_cost_and_usage(
            ce,
            {**base_kwargs, "GroupBy": group_by_specs(tag_key, include_tag=True)},
        )
    except Exception as exc:
        if not _is_tag_grouping_error(exc):
            raise
        include_tag = False
        warnings.append(
            f"tag {tag_key!r} is not an active cost-allocation tag in this "
            f"account ({exc}). Grouping by USAGE_TYPE only. Activate it with "
            f"`python scripts/daily_cost_tracker.py --activate-tag --tag {tag_key}` "
            "after resources carry the tag (CE picks it up within ~24h)."
        )
        # Drop the tag filter too — CE rejects unknown tag keys in Filter.
        fallback_filter = usage_filter(
            services, tag_key=None, tag_values=None, usage_only=usage_only
        )
        fallback_kwargs = dict(base_kwargs)
        if fallback_filter:
            fallback_kwargs["Filter"] = fallback_filter
        else:
            fallback_kwargs.pop("Filter", None)
        payload = _paginate_cost_and_usage(
            ce,
            {
                **fallback_kwargs,
                "GroupBy": group_by_specs(tag_key, include_tag=False),
            },
        )

    rows = flatten_results(payload, tag_key=tag_key, include_tag=include_tag)
    return TrackerReport(
        account=account,
        start=start,
        end=end,
        services=services,
        tag_key=tag_key,
        tag_values=list(tag_values or []),
        rows=rows,
        warnings=warnings,
        tag_grouping=include_tag,
    )


def activate_cost_allocation_tag(ce: Any, tag_key: str) -> str:
    """Activate a user-defined cost-allocation tag. Returns AWS status text."""
    try:
        resp = ce.update_cost_allocation_tags_status(
            CostAllocationTagsStatus=[{"TagKey": tag_key, "Status": "Active"}]
        )
    except Exception as exc:
        msg = str(exc)
        if "Tag keys not found" in msg or "not found" in msg.lower():
            raise RuntimeError(
                f"tag {tag_key!r} is not visible to Cost Explorer yet. Tag at "
                "least one resource with this key, wait up to 24h for the key "
                "to appear in Billing → Cost allocation tags, then retry "
                f"`--activate-tag --tag {tag_key}`."
            ) from exc
        raise
    errors = resp.get("Errors") or []
    if errors:
        raise RuntimeError(f"failed to activate tag {tag_key!r}: {errors}")
    return f"activated cost-allocation tag {tag_key!r}"


def _fmt_usd(amount: float) -> str:
    return f"${amount:,.6f}"


def render_table(report: TrackerReport) -> str:
    lines: list[str] = []
    acct = report.account or "(unknown account)"
    lines.append(
        f"Daily cost tracker  {report.start} → {report.end} (End exclusive)  {acct}"
    )
    lines.append(f"Services: {', '.join(report.services) or '(none)'}")
    lines.append(
        f"Tag: {report.tag_key}"
        + (f" values={report.tag_values}" if report.tag_values else " (all values + untagged)")
    )
    lines.append("Metric: UnblendedCost  Record type: Usage (credits excluded)")
    if report.warnings:
        lines.append("")
        for w in report.warnings:
            lines.append(f"WARNING: {w}")
    lines.append("")
    lines.append(f"Grand total  {_fmt_usd(report.grand_total())}")
    lines.append("")
    lines.append("## By tag")
    if report.tag_grouping:
        for name, cost in report.totals_by_tag():
            lines.append(f"  {_fmt_usd(cost):>14}  {name}")
    else:
        lines.append("  (tag grouping unavailable)")
    lines.append("")
    lines.append("## By usage type (Bedrock models + other Bedrock meters)")
    usage_rows = report.totals_by_usage_type()
    if not usage_rows:
        lines.append("  (no usage in period)")
    for name, cost in usage_rows:
        lines.append(f"  {_fmt_usd(cost):>14}  {name}")
    lines.append("")
    lines.append("## Daily")
    lines.append(
        f"  {'date':<12} {'cost':>14}  {'tag':<24}  usage_type"
    )
    if not report.rows:
        lines.append("  (no rows)")
    for row in report.rows:
        tag = row.keys.get("tag") or UNTAGGED
        usage = row.keys.get("usage_type") or "(none)"
        est = " *" if row.estimated else ""
        lines.append(
            f"  {row.start:<12} {_fmt_usd(row.cost):>14}  {tag:<24}  {usage}{est}"
        )
    if any(r.estimated for r in report.rows):
        lines.append("")
        lines.append("* estimated (current month, not yet finalized)")
    return "\n".join(lines) + "\n"


def report_to_json(report: TrackerReport) -> dict[str, Any]:
    return {
        "account": report.account,
        "start": report.start,
        "end": report.end,
        "services": report.services,
        "tag_key": report.tag_key,
        "tag_values": report.tag_values,
        "tag_grouping": report.tag_grouping,
        "warnings": report.warnings,
        "grand_total": report.grand_total(),
        "by_tag": [{"tag": k, "cost": v} for k, v in report.totals_by_tag()],
        "by_usage_type": [
            {"usage_type": k, "cost": v} for k, v in report.totals_by_usage_type()
        ],
        "daily_totals": [
            {"date": d, "cost": c} for d, c in report.daily_totals()
        ],
        "rows": [
            {
                "start": r.start,
                "keys": r.keys,
                "cost": r.cost,
                "quantity": r.quantity,
                "estimated": r.estimated,
            }
            for r in report.rows
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Daily Cost Explorer tracker. Pass --service and --tag as inputs; "
            "the bedrock preset covers Amazon Bedrock plus every Bedrock model "
            "meter (USAGE_TYPE)."
        )
    )
    p.add_argument(
        "--service",
        action="append",
        dest="services",
        metavar="NAME",
        help=(
            "Preset (bedrock) or CE SERVICE name. Repeatable. "
            "Default: bedrock (all Bedrock services + model usage types)."
        ),
    )
    p.add_argument(
        "--tag",
        default="tr:project-name",
        help="Cost-allocation tag key to group by (default: tr:project-name).",
    )
    p.add_argument(
        "--tag-value",
        action="append",
        dest="tag_values",
        default=None,
        help="Restrict to this tag value. Repeatable. Default: all values.",
    )
    p.add_argument("--days", type=int, default=14, help="Lookback days including today.")
    p.add_argument("--profile", default=None, help="AWS named profile.")
    p.add_argument("--region", default="us-east-1", help="CE endpoint region.")
    p.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        dest="fmt",
    )
    p.add_argument(
        "--include-credits",
        action="store_true",
        help="Do not filter RECORD_TYPE=Usage (net of credits/refunds).",
    )
    p.add_argument(
        "--activate-tag",
        action="store_true",
        help="Activate --tag as a cost-allocation tag, then exit (or continue if also reporting).",
    )
    p.add_argument(
        "--activate-only",
        action="store_true",
        help="Only activate --tag; do not query costs.",
    )
    return p


def _boto_session(profile: str | None, region: str):
    import boto3

    return boto3.Session(profile_name=profile, region_name=region)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session = _boto_session(args.profile, args.region)
    ce = session.client("ce")
    sts = session.client("sts")
    account = sts.get_caller_identity()["Account"]

    if args.activate_tag or args.activate_only:
        print(activate_cost_allocation_tag(ce, args.tag), file=sys.stderr)
        if args.activate_only:
            return 0

    start, end = period_for_days(args.days)
    requested = args.services or ["bedrock"]
    available = list_services(ce, start, end)
    services = resolve_services(requested, available)
    report = query_daily_costs(
        ce,
        start=start,
        end=end,
        services=services,
        tag_key=args.tag,
        tag_values=args.tag_values,
        usage_only=not args.include_credits,
        account=account,
    )
    if args.fmt == "json":
        sys.stdout.write(json.dumps(report_to_json(report), indent=2) + "\n")
    else:
        sys.stdout.write(render_table(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover — CLI boundary
        print(f"daily_cost_tracker: {exc}", file=sys.stderr)
        raise SystemExit(1)
