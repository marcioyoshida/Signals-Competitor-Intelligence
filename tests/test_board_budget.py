"""#329: the board's budget and health alarms (ADR 030 pattern) and the per-run metric line."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))

from src.board import handler  # noqa: E402

CDK_JSON = json.loads((ROOT / "infra" / "cdk.json").read_text(encoding="utf-8"))
TOPIC = "arn:aws:sns:us-east-1:111111111111:Main-OncaAlertsTopic-XYZ"


def test_metric_line_splits_hosted_from_client_billed(capsys):
    u = {"calls": 12, "tokens_in": 300_000, "tokens_out": 20_000}
    hosted = handler.emit_run_metrics(u, client_billed=False, capped=True, status="partial")
    client = handler.emit_run_metrics(u, client_billed=True, capped=False, status="complete")
    assert (hosted["HostedTokens"], hosted["ClientTokens"], hosted["CappedRuns"]) == (320_000, 0, 1)
    assert (client["HostedTokens"], client["ClientTokens"], client["CappedRuns"]) == (0, 320_000, 0)
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    m = lines[0]["_aws"]["CloudWatchMetrics"][0]
    assert m["Namespace"] == "Onca/Board" and m["Dimensions"] == [[]]          # no per-tenant metrics
    assert {x["Name"] for x in m["Metrics"]} == {"HostedTokens", "ClientTokens", "Runs", "CappedRuns"}
    assert "tenant" not in lines[0]


def test_budget_constants_sit_below_their_limits():
    pytest.importorskip("aws_cdk")
    import board_stack as b
    assert 0.5 <= b.BUDGET_ALARM_AT < 1 and 0.6 <= b.DURATION_ALARM_AT < 1
    assert b.HOSTED_DAILY_TOKEN_BUDGET >= 2 * 400_000                       # at least two capped runs


@pytest.fixture(scope="module")
def resources(tmp_path_factory):
    cdk = pytest.importorskip("aws_cdk")
    from aws_cdk import assertions
    from aws_cdk import aws_lambda as lambda_
    import board_stack
    names = board_stack.board_names({**CDK_JSON["context"]["board"], "alerts_topic_arn": TOPIC})
    asset = tmp_path_factory.mktemp("lambda")
    (asset / "placeholder.py").write_text("", encoding="utf-8")
    app = cdk.App(outdir=str(tmp_path_factory.mktemp("cdk.out")))
    stack = board_stack.OncaBoardStack(app, "OncaBoardStack", runtime=lambda_.Runtime.PYTHON_3_13,
                                       asset=asset, env=cdk.Environment(account="111111111111",
                                                                        region="us-east-1"), **names)
    return assertions.Template.from_stack(stack).to_json()["Resources"]


def _alarms(resources):
    return {r["Properties"]["AlarmName"]: r["Properties"] for r in resources.values()
            if r["Type"] == "AWS::CloudWatch::Alarm"}


def test_every_alarm_exists_named_described_and_routed_both_ways(resources):
    import board_stack
    alarms = _alarms(resources)
    assert set(alarms) == set(board_stack.ALARMS)
    for name, a in alarms.items():
        assert a["AlarmActions"] == [TOPIC] and a["OKActions"] == [TOPIC], name
        assert len(a["AlarmDescription"]) > 80 and "Look:" in a["AlarmDescription"], name
        assert a["TreatMissingData"] == "notBreaching", name


def test_hosted_budget_and_duration_thresholds(resources):
    import board_stack as b
    a = _alarms(resources)
    assert a["Onca-BoardHostedTokens"]["Threshold"] == b.HOSTED_DAILY_TOKEN_BUDGET * b.BUDGET_ALARM_AT
    assert a["Onca-BoardHostedTokens"]["Period"] == 86400
    assert a["Onca-BoardHostedTokens"]["MetricName"] == "HostedTokens"   # never ClientTokens
    assert a["Onca-BoardRunDuration"]["Threshold"] == 15 * 60_000 * b.DURATION_ALARM_AT
    exprs = [m.get("Expression") for m in a["Onca-BoardRunFailures"]["Metrics"]]
    assert any(e and "FILL(f, 0)" in e for e in exprs)
    assert len(a["Onca-BoardRunFailures"]["Metrics"]) <= 10              # CloudWatch math limit


def test_alarms_add_no_iam_and_no_topic(resources):
    assert not [r for r in resources.values() if r["Type"] == "AWS::SNS::Topic"]
    policies = [r for r in resources.values() if r["Type"] == "AWS::IAM::Policy"]
    text = json.dumps(policies)
    assert "sns:" not in text.lower() and "cloudwatch:" not in text.lower()


def test_cdk_json_carries_the_alerts_topic():
    pytest.importorskip("aws_cdk")
    import board_stack
    assert board_stack.board_names(CDK_JSON["context"]["board"])["alerts_topic_arn"].startswith(
        "arn:aws:sns:")
