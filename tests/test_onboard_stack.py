"""#379: OncaOnboardStack — the fixed-name function, least-privilege IAM, no resource permission.

Synthesizes the stack on its own into a bare app (seconds, no AWS calls), as the identity tests do.
"""
from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))

cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402
from aws_cdk import aws_lambda as lambda_  # noqa: E402

import onboard_stack  # noqa: E402
from src.dashboard import onboard  # noqa: E402
from src.dashboard import tenant_config as tc  # noqa: E402

NAMES = {"tenant_config_table": "Main-OncaTenantConfigAAAA-1111",
         "federated_map_table": "Main-OncaFederatedTenantMapTableBBBB-2222",
         "user_pool_id": "us-east-1_TestPool1"}
APP_SRC = (ROOT / "infra" / "app.py").read_text(encoding="utf-8")
CDK_JSON = json.loads((ROOT / "infra" / "cdk.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def resources(tmp_path_factory):
    asset = tmp_path_factory.mktemp("lambda")
    (asset / "placeholder.py").write_text("", encoding="utf-8")
    app = cdk.App(outdir=str(tmp_path_factory.mktemp("cdk.out")))
    stack = onboard_stack.OncaOnboardStack(
        app, "OncaOnboardStack", runtime=lambda_.Runtime.PYTHON_3_13, asset=asset,
        env=cdk.Environment(account="111111111111", region="us-east-1"), **NAMES)
    return assertions.Template.from_stack(stack).to_json()["Resources"]


def _of(resources, kind):
    return [r for r in resources.values() if r["Type"] == kind]


def test_the_stack_is_three_resources_and_nothing_public(resources):
    assert sorted(r["Type"] for r in resources.values() if r["Type"] != "AWS::CDK::Metadata") == [
        "AWS::IAM::Policy", "AWS::IAM::Role", "AWS::Lambda::Function"]
    # no Function URL, no route, and NO resource-based permission (fleet-monitor's role does not
    # exist yet; Lambda rejects an unknown principal, which would fail this deploy)
    assert not _of(resources, "AWS::Lambda::Permission") and not _of(resources, "AWS::Lambda::Url")


def test_the_function_has_the_fixed_name_and_the_three_environment_names(resources):
    (fn,) = _of(resources, "AWS::Lambda::Function")
    p = fn["Properties"]
    assert p["FunctionName"] == "onca-onboard" == onboard.FUNCTION == onboard_stack.FUNCTION_NAME
    assert p["Handler"] == "src.dashboard.onboard.lambda_handler"
    assert callable(onboard.lambda_handler)
    assert p["Runtime"] == "python3.13" and p["ReservedConcurrentExecutions"] == 1
    assert p["Environment"]["Variables"] == {
        "PYTHONPATH": "/var/task",
        "ONCA_TENANT_CONFIG_TABLE": NAMES["tenant_config_table"],
        "ONCA_FEDERATED_MAP_TABLE": NAMES["federated_map_table"],
        "ONCA_USER_POOL_ID": NAMES["user_pool_id"]}


def test_iam_is_exactly_the_two_tables_and_the_one_pool(resources):
    (policy,) = _of(resources, "AWS::IAM::Policy")
    by_sid = {s["Sid"]: s for s in policy["Properties"]["PolicyDocument"]["Statement"]}
    assert set(by_sid) == {"TenantConfigRows", "FederatedMapRows", "PoolLogins"}
    assert all(s["Effect"] == "Allow" for s in by_sid.values())

    def arn(sid):   # one resource per statement; flatten CDK's Fn::Join with the partition ref
        res = by_sid[sid]["Resource"]
        assert isinstance(res, dict), "exactly one resource"
        return "".join(p if isinstance(p, str) else "{aws}" for p in res["Fn::Join"][1])

    assert arn("TenantConfigRows") == ("arn:{aws}:dynamodb:us-east-1:111111111111:table/"
                                       + NAMES["tenant_config_table"])
    assert arn("FederatedMapRows") == ("arn:{aws}:dynamodb:us-east-1:111111111111:table/"
                                       + NAMES["federated_map_table"])
    assert arn("PoolLogins") == ("arn:{aws}:cognito-idp:us-east-1:111111111111:userpool/"
                                 + NAMES["user_pool_id"])
    assert sorted(by_sid["TenantConfigRows"]["Action"]) == [
        "dynamodb:DeleteItem", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    assert sorted(by_sid["FederatedMapRows"]["Action"]) == [
        "dynamodb:DeleteItem", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Scan",
        "dynamodb:UpdateItem"]
    assert sorted(by_sid["PoolLogins"]["Action"]) == sorted(onboard_stack.COGNITO_ACTIONS)
    assert "*" not in json.dumps(policy)
    # the role's only managed policy is the basic execution one (its own log group)
    (role,) = _of(resources, "AWS::IAM::Role")
    managed = json.dumps(role["Properties"]["ManagedPolicyArns"])
    assert managed.count("service-role/") == 1 and "AWSLambdaBasicExecutionRole" in managed


def test_the_cognito_grant_is_exactly_the_calls_the_code_makes():
    """Drift guard: a new admin_* call needs its action, an unused action must go."""
    src = inspect.getsource(onboard) + inspect.getsource(tc.cognito_upsert_user) + inspect.getsource(
        tc.cognito_user_link)
    called = {"cognito-idp:" + "".join(w.title() for w in m.split("_"))
              for m in re.findall(r"\.(admin_[a-z_]+)\(", src)}
    assert called == set(onboard_stack.COGNITO_ACTIONS)
    assert re.findall(r"\.scan\(", inspect.getsource(onboard)) == [".scan("]   # one, in _scan
    assert "_scan(ctx.fed)" in inspect.getsource(onboard) and "_scan(ctx.tenants)" not in inspect.getsource(onboard)


def test_the_names_come_from_context_and_a_malformed_one_fails_the_synth():
    assert onboard_stack.onboard_names(None) is None and onboard_stack.onboard_names({}) is None
    assert onboard_stack.onboard_names(NAMES) == NAMES
    with pytest.raises(ValueError, match="user_pool_id"):
        onboard_stack.onboard_names({**NAMES, "user_pool_id": "arn:aws:cognito-idp:::userpool/x"})
    with pytest.raises(ValueError, match="federated_map_table"):
        onboard_stack.onboard_names({k: v for k, v in NAMES.items() if k != "federated_map_table"})


def test_the_app_wires_the_stack_by_name_and_adds_nothing_to_the_main_stack():
    names = onboard_stack.onboard_names(CDK_JSON["context"]["onboard"])   # cdk.json is well-formed
    assert set(names) == set(onboard_stack.CONTEXT_FIELDS)
    assert 'OncaOnboardStack(app, "OncaOnboardStack", runtime=LAMBDA_RUNTIME, asset=LAMBDA_ASSET,' in APP_SRC
    wiring = APP_SRC[APP_SRC.index("from onboard_stack import"):APP_SRC.index("Tags.of(app)")]
    assert "main_stack" not in wiring   # no token from OncaPrototypeStack => no cross-stack export
    assert "self.tenant_config_table" not in APP_SRC and "self.federated_map_table" not in APP_SRC
