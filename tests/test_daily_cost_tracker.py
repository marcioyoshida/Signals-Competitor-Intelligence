"""Unit tests for the generic Cost Explorer daily tracker."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import daily_cost_tracker as t


class FakeCE:
    def __init__(
        self,
        *,
        services=None,
        pages=None,
        tag_error=False,
        activate_errors=None,
    ):
        self.services = services or ["Amazon Bedrock", "AWS Lambda", "Amazon S3"]
        self.pages = list(pages or [])
        self.tag_error = tag_error
        self.activate_errors = activate_errors
        self.calls = []

    def get_dimension_values(self, **kwargs):
        self.calls.append(("get_dimension_values", kwargs))
        return {"DimensionValues": [{"Value": s} for s in self.services]}

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(("get_cost_and_usage", kwargs))
        grouped_by_tag = any(g.get("Type") == "TAG" for g in kwargs.get("GroupBy") or [])
        if self.tag_error and grouped_by_tag:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "Group Definition Tag is invalid",
                    }
                },
                "GetCostAndUsage",
            )
        if not self.pages:
            return {"GroupDefinitions": kwargs.get("GroupBy") or [], "ResultsByTime": []}
        page = dict(self.pages.pop(0))
        page.setdefault("GroupDefinitions", kwargs.get("GroupBy") or [])
        return page

    def update_cost_allocation_tags_status(self, **kwargs):
        self.calls.append(("update_cost_allocation_tags_status", kwargs))
        if self.activate_errors is not None:
            return {"Errors": self.activate_errors}
        return {"Errors": []}


def _ce_page(groups, start="2026-08-24", estimated=True, extra_defs=None):
    defs = extra_defs or [
        {"Type": "TAG", "Key": "tr:project-name"},
        {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
    ]
    return {
        "GroupDefinitions": defs,
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": start, "End": "2026-08-25"},
                "Estimated": estimated,
                "Groups": groups,
            }
        ],
    }


def _group(keys, cost, qty=0.0):
    return {
        "Keys": keys,
        "Metrics": {
            "UnblendedCost": {"Amount": str(cost), "Unit": "USD"},
            "UsageQuantity": {"Amount": str(qty), "Unit": "N/A"},
        },
    }


def test_bedrock_preset_matches_all_bedrock_services():
    available = [
        "Amazon Bedrock",
        "Amazon Bedrock AgentCore",
        "AWS Lambda",
        "Amazon Bedrock Agent",
    ]
    got = t.resolve_services(["bedrock"], available)
    assert got == [
        "Amazon Bedrock",
        "Amazon Bedrock AgentCore",
        "Amazon Bedrock Agent",
    ]


def test_resolve_services_exact_and_partial():
    available = ["Amazon Bedrock", "AWS Lambda"]
    assert t.resolve_services(["Amazon Bedrock"], available) == ["Amazon Bedrock"]
    assert t.resolve_services(["lambda"], available) == ["AWS Lambda"]
    with pytest.raises(ValueError, match="not found"):
        t.resolve_services(["redshift"], available)


def test_usage_filter_and_group_by():
    filt = t.usage_filter(
        ["Amazon Bedrock"],
        tag_key="tr:project-name",
        tag_values=["onca"],
        usage_only=True,
    )
    assert filt["And"][0]["Dimensions"]["Key"] == "SERVICE"
    assert filt["And"][1]["Dimensions"]["Values"] == ["Usage"]
    assert filt["And"][2]["Tags"]["Key"] == "tr:project-name"
    groups = t.group_by_specs("tr:project-name", include_tag=True)
    assert groups[0] == {"Type": "TAG", "Key": "tr:project-name"}
    assert groups[1] == {"Type": "DIMENSION", "Key": "USAGE_TYPE"}
    assert t.group_by_specs("tr:project-name", include_tag=False) == [
        {"Type": "DIMENSION", "Key": "USAGE_TYPE"}
    ]


def test_period_for_days_end_exclusive():
    from datetime import date

    start, end = t.period_for_days(14, today=date(2026, 8, 24))
    assert start == "2026-08-11"
    assert end == "2026-08-25"


def test_normalize_tag_value_untagged_sentinel():
    assert t.normalize_tag_value("", "tr:project-name") == t.UNTAGGED
    assert t.normalize_tag_value("tr:project-name$", "tr:project-name") == t.UNTAGGED
    assert t.normalize_tag_value("onca", "tr:project-name") == "onca"


def test_flatten_and_totals():
    payload = _ce_page(
        [
            _group(["onca", "USE1-NovaLite-input-tokens"], 0.0118, 196.7),
            _group(["onca", "USE1-NovaLite-output-tokens"], 0.0120, 50.1),
            _group(["tr:project-name$", "USE1-TitanEmbeddingV2-Text-input-tokens"], 0.002, 1.0),
        ]
    )
    rows = t.flatten_results(payload, tag_key="tr:project-name", include_tag=True)
    assert len(rows) == 3
    report = t.TrackerReport(
        account="668449743071",
        start="2026-08-11",
        end="2026-08-25",
        services=["Amazon Bedrock"],
        tag_key="tr:project-name",
        tag_values=[],
        rows=rows,
    )
    assert report.grand_total() == pytest.approx(0.0258)
    assert report.totals_by_tag() == [
        ("onca", pytest.approx(0.0238)),
        (t.UNTAGGED, pytest.approx(0.002)),
    ]
    by_usage = dict(report.totals_by_usage_type())
    assert "USE1-NovaLite-input-tokens" in by_usage


def test_query_falls_back_when_tag_not_activated():
    page = _ce_page(
        [_group(["USE1-NovaLite-input-tokens"], 0.05, 10)],
        extra_defs=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    )
    ce = FakeCE(tag_error=True, pages=[page])
    report = t.query_daily_costs(
        ce,
        start="2026-08-11",
        end="2026-08-25",
        services=["Amazon Bedrock"],
        tag_key="tr:project-name",
        account="1",
    )
    assert report.tag_grouping is False
    assert report.warnings
    assert report.grand_total() == pytest.approx(0.05)
    # first call used TAG group-by, second dropped it
    grouped = [c for c in ce.calls if c[0] == "get_cost_and_usage"]
    assert any(any(g.get("Type") == "TAG" for g in grouped[0][1]["GroupBy"]) for _ in [0])
    assert all(g.get("Type") != "TAG" for g in grouped[1][1]["GroupBy"])


def test_query_groups_by_tag_when_active():
    page = _ce_page([_group(["onca", "USE1-NovaLite-input-tokens"], 0.01, 1)])
    ce = FakeCE(pages=[page])
    report = t.query_daily_costs(
        ce,
        start="2026-08-11",
        end="2026-08-25",
        services=["Amazon Bedrock"],
        tag_key="tr:project-name",
    )
    assert report.tag_grouping is True
    assert report.rows[0].keys["tag"] == "onca"
    filt = ce.calls[0][1]["Filter"]
    # Usage-only, no tag-value restriction
    assert filt["And"][1]["Dimensions"]["Values"] == ["Usage"]


def test_activate_tag_success_and_error():
    ce = FakeCE()
    assert "tr:project-name" in t.activate_cost_allocation_tag(ce, "tr:project-name")
    body = ce.calls[0][1]["CostAllocationTagsStatus"][0]
    assert body == {"TagKey": "tr:project-name", "Status": "Active"}
    ce_err = FakeCE(activate_errors=[{"TagKey": "tr:project-name", "Code": "Invalid"}])
    with pytest.raises(RuntimeError, match="failed to activate"):
        t.activate_cost_allocation_tag(ce_err, "tr:project-name")


def test_render_table_and_json_include_inputs():
    rows = [
        t.CostRow(
            start="2026-08-24",
            keys={"tag": "onca", "usage_type": "USE1-NovaLite-input-tokens"},
            cost=0.01,
            quantity=10,
            estimated=True,
        )
    ]
    report = t.TrackerReport(
        account="668449743071",
        start="2026-08-11",
        end="2026-08-25",
        services=["Amazon Bedrock"],
        tag_key="tr:project-name",
        tag_values=[],
        rows=rows,
    )
    table = t.render_table(report)
    assert "Amazon Bedrock" in table
    assert "tr:project-name" in table
    assert "onca" in table
    assert "USE1-NovaLite-input-tokens" in table
    payload = t.report_to_json(report)
    assert payload["tag_key"] == "tr:project-name"
    assert payload["grand_total"] == pytest.approx(0.01)
    json.dumps(payload)  # serializable


def test_cli_parser_defaults_match_requested_inputs():
    args = t.build_parser().parse_args([])
    assert args.tag == "tr:project-name"
    assert args.services is None  # main() fills bedrock
    args2 = t.build_parser().parse_args(
        ["--service", "bedrock", "--tag", "tr:project-name", "--tag-value", "onca"]
    )
    assert args2.services == ["bedrock"]
    assert args2.tag == "tr:project-name"
    assert args2.tag_values == ["onca"]
