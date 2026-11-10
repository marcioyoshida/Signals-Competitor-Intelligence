"""#379: the operator onboarding Lambda (``onca-onboard``), in its own small stack.

Own stack because ``OncaPrototypeStack`` is at 485 of CloudFormation's 500 resources (counted
live 2026-11-10); this one is three (function, role, policy). See ``src/dashboard/onboard.py``
for the handler and the request contract.

**Fixed function name.** Fleet-monitor's ``OnboardFn`` role is allowed ``lambda:InvokeFunction``
on exactly ``arn:aws:lambda:<region>:<account>:function:onca-onboard`` (an identity policy on ITS
side, same account). So there is deliberately NO resource-based permission here: that role does
not exist yet, Lambda rejects a principal it cannot find, and this deploy must not wait on
fleet-monitor's.

**The main stack's resources come in by NAME**, from the ``onboard`` context in ``cdk.json``,
not as CDK tokens (what ``OncaGdeltBridgeStack`` does for the entities table). A token would add
``Export`` outputs to ``OncaPrototypeStack`` and make CloudFormation refuse any later change to
an exported value while this stack imports it — the pool was already replaced once (#262, V2) —
and it would make every deploy of this stack deploy the main stack first. By name, this stack
adds nothing to ``OncaPrototypeStack`` and the two deploy independently. The cost: if the tables
or the pool are ever replaced, the three names in ``cdk.json`` must follow (the function then
fails loudly with AccessDenied / ResourceNotFound, never silently). Read them from the stack::

    aws cloudformation describe-stacks --stack-name OncaPrototypeStack \\
      --query "Stacks[0].Outputs[?OutputKey=='TenantConfigTable'||OutputKey=='OncaUserPoolV2Id']"
    aws cloudformation list-stack-resources --stack-name OncaPrototypeStack \\
      --query "StackResourceSummaries[?contains(LogicalResourceId,'FederatedTenantMap')].PhysicalResourceId"

Deploy (after staging ``build/lambda`` exactly as for the main stack)::

    cd infra && cdk deploy OncaOnboardStack --exclusively

then re-run ``scripts/privacy_retention.sh`` so ``/aws/lambda/onca-onboard`` gets the 90-day
log retention the privacy policy promises.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

FUNCTION_NAME = "onca-onboard"
CONTEXT_KEY = "onboard"
CONTEXT_FIELDS = ("tenant_config_table", "federated_map_table", "user_pool_id")
_TABLE_RE = re.compile(r"[A-Za-z0-9_.-]{3,255}")
_POOL_RE = re.compile(r"[\w-]+_[0-9A-Za-z]+")

# Exactly what src/dashboard/onboard.py (and the tenant_config functions it calls) uses.
TENANT_CONFIG_ACTIONS = ("dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                         "dynamodb:DeleteItem")
# + Scan: expire/remove/get list the logins onboarding created for one tenant.
FEDERATED_MAP_ACTIONS = TENANT_CONFIG_ACTIONS + ("dynamodb:Scan",)
COGNITO_ACTIONS = (
    "cognito-idp:AdminGetUser",               # the relink guard, and the user's opaque id
    "cognito-idp:AdminCreateUser",            # create (invitation suppressed or sent) and RESEND
    "cognito-idp:AdminUpdateUserAttributes",  # custom:tier on an existing user
    "cognito-idp:AdminDisableUser",           # expire, mode "disable"
    "cognito-idp:AdminEnableUser",            # a put/link that wakes a login expire disabled
    "cognito-idp:AdminDeleteUser",            # expire mode "delete", remove
)


def onboard_names(context: Any) -> dict[str, str] | None:
    """The three physical names from the ``onboard`` context, or None when it is absent (the
    stack is then left out of the app). A present but malformed context is an error."""
    if not context:
        return None
    names = {k: str((context or {}).get(k) or "") for k in CONTEXT_FIELDS}
    for key in ("tenant_config_table", "federated_map_table"):
        if not _TABLE_RE.fullmatch(names[key]):
            raise ValueError(f"cdk context {CONTEXT_KEY}.{key} is not a DynamoDB table name: {names[key]!r}")
    if not _POOL_RE.fullmatch(names["user_pool_id"]):
        raise ValueError(f"cdk context {CONTEXT_KEY}.user_pool_id is not a user pool id: "
                         f"{names['user_pool_id']!r}")
    return names


class OncaOnboardStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, runtime: lambda_.Runtime,
                 asset: Path | str, tenant_config_table: str, federated_map_table: str,
                 user_pool_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)

        fn = lambda_.Function(
            self, "OncaOnboard",
            function_name=FUNCTION_NAME,   # fixed: fleet-monitor's role is scoped to this ARN
            runtime=runtime,
            handler="src.dashboard.onboard.lambda_handler",
            code=lambda_.Code.from_asset(str(asset)),
            timeout=Duration.seconds(30),
            memory_size=256,
            # one request at a time: put's "exists? stamped? billed?" checks and its write are
            # then never interleaved with another panel request
            reserved_concurrent_executions=1,
            environment={"PYTHONPATH": "/var/task",
                         "ONCA_TENANT_CONFIG_TABLE": tenant_config_table,
                         "ONCA_FEDERATED_MAP_TABLE": federated_map_table,
                         "ONCA_USER_POOL_ID": user_pool_id},
        )

        def table_arn(name: str) -> str:
            return self.format_arn(service="dynamodb", resource="table", resource_name=name)

        # Least privilege, spelled out rather than grant_read_write_data (which, on a table
        # imported by name, also grants every index, the streams and the batch calls).
        fn.add_to_role_policy(iam.PolicyStatement(
            sid="TenantConfigRows", actions=list(TENANT_CONFIG_ACTIONS),
            resources=[table_arn(tenant_config_table)]))
        fn.add_to_role_policy(iam.PolicyStatement(
            sid="FederatedMapRows", actions=list(FEDERATED_MAP_ACTIONS),
            resources=[table_arn(federated_map_table)]))
        fn.add_to_role_policy(iam.PolicyStatement(
            sid="PoolLogins", actions=list(COGNITO_ACTIONS),
            resources=[self.format_arn(service="cognito-idp", resource="userpool",
                                       resource_name=user_pool_id)]))
        # No add_permission for fleet-monitor: see the module docstring.
        self.function = fn
        CfnOutput(self, "OnboardFunctionName", value=fn.function_name)
        CfnOutput(self, "OnboardFunctionArn", value=fn.function_arn)
