"""#322 OncaBoardStack: the board brief orchestrator (ADR 031) in its own stack.

Own stack because OncaPrototypeStack is near CloudFormation's 500-resource limit. Like
OncaOnboardStack, the main stack's resources come in by NAME from the ``board`` context in
cdk.json (never as tokens), so this stack adds no exports to the main one and the two deploy
independently. If a named resource is replaced, the context must follow; the task then fails
loudly (AccessDenied / ResourceNotFound), never silently.

Resources: a pack bucket, the ``OncaBoardRuns`` table, one task function with a fixed role name
(``onca-board-runner``: the main stack's BYOK broker role trusts it by ARN, #350), and a Standard
state machine Admit → Pack → Run, with a catch that marks the run failed.

Deploy (after staging ``build/lambda`` exactly as for the main stack)::

    cd infra && cdk deploy OncaBoardStack --exclusively
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

CONTEXT_KEY = "board"
FUNCTION_NAME = "onca-board-task"
ROLE_NAME = "onca-board-runner"   # fixed: the BYOK broker's trust policy names this ARN
STATE_MACHINE_NAME = "onca-board-brief"
RETENTION_DAYS = 90               # privacy policy: logs and run data (ADR 031 open question)
RUN_TIMEOUT = Duration.minutes(15)

# #329 budgets (ADR 030 layer 2: alarm on the distance to a limit, not on the limit).
# Onça-billed (hosted) tokens are spent only for an active paid plan or an owner flag; client-
# model runs bill the tenant and are not budgeted here. 2M/day ≈ five capped runs.
HOSTED_DAILY_TOKEN_BUDGET = 2_000_000
BUDGET_ALARM_AT = 0.75
DURATION_ALARM_AT = 0.8            # of RUN_TIMEOUT, the Lambda's hard limit
CAPPED_RUNS_PER_DAY = 3
ALARMS = ("Onca-BoardHostedTokens", "Onca-BoardRunFailures", "Onca-BoardRunDuration",
          "Onca-BoardCappedRuns")

CONTEXT_FIELDS = ("site_bucket", "tenant_config_table", "api_usage_table", "model_keys_table",
                  "model_key_kms_arn", "broker_role_arn", "guardrail_id", "guardrail_version",
                  "synth_model_id", "alerts_topic_arn")
_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{3,255}")
_ARN_RE = re.compile(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:[\w/:.-]+")


def board_names(context: Any) -> dict[str, Any] | None:
    """The validated ``board`` context, or None when absent (the stack is then left out)."""
    if not context:
        return None
    c = dict(context)
    out: dict[str, Any] = {}
    for k in CONTEXT_FIELDS:
        v = str(c.get(k) or "")
        ok = (_ARN_RE.fullmatch(v) if k.endswith("_arn") else
              re.fullmatch(r"\d{1,4}|DRAFT", v) if k == "guardrail_version" else _NAME_RE.fullmatch(v))
        if not ok:
            raise ValueError(f"cdk context {CONTEXT_KEY}.{k} is missing or malformed: {v!r}")
        out[k] = v
    profiles = c.get("model_profiles") or {}
    if not isinstance(profiles, dict) or not all(_ARN_RE.fullmatch(str(a)) for a in profiles.values()):
        raise ValueError(f"cdk context {CONTEXT_KEY}.model_profiles must map model ids to ARNs")
    out["model_profiles"] = profiles
    # "runs_table" is read by the MAIN stack (#328, act_fn), not by this one: it is this stack's
    # own output, recorded after the first deploy.
    return out


class OncaBoardStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, runtime: lambda_.Runtime,
                 asset: Path | str, site_bucket: str, tenant_config_table: str,
                 api_usage_table: str, model_keys_table: str, model_key_kms_arn: str,
                 broker_role_arn: str, guardrail_id: str, guardrail_version: str,
                 synth_model_id: str, model_profiles: dict[str, str], alerts_topic_arn: str,
                 **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)

        packs = s3.Bucket(
            self, "BoardPacks",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[s3.LifecycleRule(prefix="board/", expiration=Duration.days(RETENTION_DAYS))],
            removal_policy=RemovalPolicy.RETAIN)

        runs = ddb.Table(
            self, "OncaBoardRuns",
            partition_key=ddb.Attribute(name="tenant", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.RETAIN)

        role = iam.Role(
            self, "BoardRunnerRole", role_name=ROLE_NAME,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole")])

        log_group = logs.LogGroup(self, "BoardTaskLogs", log_group_name=f"/aws/lambda/{FUNCTION_NAME}",
                                  retention=logs.RetentionDays.THREE_MONTHS,
                                  removal_policy=RemovalPolicy.RETAIN)

        fn = lambda_.Function(
            self, "BoardTask",
            function_name=FUNCTION_NAME,
            runtime=runtime,
            handler="src.board.handler.lambda_handler",
            code=lambda_.Code.from_asset(str(asset)),
            role=role,
            log_group=log_group,
            timeout=RUN_TIMEOUT,
            memory_size=1024,
            # a run is 15–25 large-context calls: two at a time is the blast radius
            reserved_concurrent_executions=2,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_BOARD_RUNS_TABLE": runs.table_name,
                "ONCA_BOARD_BUCKET": packs.bucket_name,
                "ONCA_SITE_BUCKET": site_bucket,
                "ONCA_TENANT_CONFIG_TABLE": tenant_config_table,
                "ONCA_API_USAGE_TABLE": api_usage_table,
                "ONCA_MODEL_KEYS_TABLE": model_keys_table,
                "ONCA_MODEL_KEY_KMS": model_key_kms_arn,
                "ONCA_MODEL_BROKER_ROLE": broker_role_arn,
                "ONCA_GUARDRAIL_ID": guardrail_id,
                "ONCA_GUARDRAIL_VERSION": guardrail_version,
                "ONCA_SYNTH_MODEL_ID": synth_model_id,
                "MODEL_PROFILES": json.dumps(model_profiles, sort_keys=True),
            })

        def table_arn(name: str) -> str:
            return self.format_arn(service="dynamodb", resource="table", resource_name=name)

        # Least privilege, spelled out (no grant_* on tables imported by name).
        role.add_to_policy(iam.PolicyStatement(
            sid="RunsTable", actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                                      "dynamodb:Query"], resources=[runs.table_arn]))
        role.add_to_policy(iam.PolicyStatement(
            sid="Packs", actions=["s3:GetObject", "s3:PutObject"],
            resources=[packs.arn_for_objects("board/packs/*")]))
        role.add_to_policy(iam.PolicyStatement(
            sid="Feed", actions=["s3:GetObject"],
            resources=[f"arn:aws:s3:::{site_bucket}/feed.json"]))
        role.add_to_policy(iam.PolicyStatement(
            sid="TenantConfig", actions=["dynamodb:GetItem"], resources=[table_arn(tenant_config_table)]))
        role.add_to_policy(iam.PolicyStatement(
            sid="UsageMeter", actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
            resources=[table_arn(api_usage_table)]))
        role.add_to_policy(iam.PolicyStatement(
            sid="ModelKeyRow", actions=["dynamodb:GetItem"], resources=[table_arn(model_keys_table)]))
        role.add_to_policy(iam.PolicyStatement(
            sid="ModelKeyDecrypt", actions=["kms:Decrypt"], resources=[model_key_kms_arn],
            conditions={"StringEquals": {"kms:EncryptionContext:onca-purpose": "model-key"}}))
        role.add_to_policy(iam.PolicyStatement(
            sid="ClientModelBroker", actions=["sts:AssumeRole"], resources=[broker_role_arn]))
        role.add_to_policy(iam.PolicyStatement(
            sid="HostedModel", actions=["bedrock:InvokeModel", "bedrock:Converse"],
            resources=[f"arn:aws:bedrock:{self.region}::foundation-model/*",
                       f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                       *sorted(set(model_profiles.values()))]))
        role.add_to_policy(iam.PolicyStatement(
            sid="Guardrail", actions=["bedrock:ApplyGuardrail"],
            resources=[f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/{guardrail_id}"]))

        admit = tasks.LambdaInvoke(self, "Admit", lambda_function=fn,
                                   payload=sfn.TaskInput.from_object({
                                       "action": "admit",
                                       "tenant": sfn.JsonPath.string_at("$.tenant"),
                                       "idempotency_key": sfn.JsonPath.string_at("$.idempotency_key"),
                                       "target": sfn.JsonPath.string_at("$.target"),
                                       "task": sfn.JsonPath.string_at("$.task"),
                                       "requested_by": sfn.JsonPath.string_at("$.requested_by")}),
                                   payload_response_only=True,
                                   task_timeout=sfn.Timeout.duration(Duration.seconds(30)))
        pack = tasks.LambdaInvoke(self, "Pack", lambda_function=fn,
                                  payload=sfn.TaskInput.from_object({
                                      "action": "pack",
                                      "tenant": sfn.JsonPath.string_at("$.tenant"),
                                      "run_id": sfn.JsonPath.string_at("$.run_id"),
                                      "target": sfn.JsonPath.string_at("$.target"),
                                      "token_cap": sfn.JsonPath.number_at("$.token_cap"),
                                      "client_billed": sfn.JsonPath.string_at("$.client_billed")}),
                                  payload_response_only=True,
                                  task_timeout=sfn.Timeout.duration(Duration.minutes(2)))
        run_task = tasks.LambdaInvoke(self, "Run", lambda_function=fn,
                                      payload=sfn.TaskInput.from_object({
                                          "action": "run",
                                          "tenant": sfn.JsonPath.string_at("$.tenant"),
                                          "run_id": sfn.JsonPath.string_at("$.run_id"),
                                          "pack_sha": sfn.JsonPath.string_at("$.pack_sha"),
                                          "token_cap": sfn.JsonPath.number_at("$.token_cap")}),
                                      payload_response_only=True,
                                      task_timeout=sfn.Timeout.duration(RUN_TIMEOUT))
        mark_failed = tasks.LambdaInvoke(self, "MarkFailed", lambda_function=fn,
                                         payload=sfn.TaskInput.from_object({
                                             "action": "fail",
                                             "tenant": sfn.JsonPath.string_at("$.tenant"),
                                             "run_id": sfn.JsonPath.string_at("$.run_id"),
                                             "error": sfn.JsonPath.json_to_string(sfn.JsonPath.object_at("$.error"))}),
                                         payload_response_only=True)
        failed = mark_failed.next(sfn.Fail(self, "RunFailed"))
        # Input (from fleet-monitor or the act API, #328): tenant, idempotency_key, target, task,
        # requested_by — all five required.
        # A catch keeps the run's ids next to the error, so MarkFailed can find the run.
        for t in (pack, run_task):
            t.add_catch(failed, result_path="$.error")

        definition = admit.next(
            sfn.Choice(self, "Admitted?")
            .when(sfn.Condition.boolean_equals("$.allowed", True),
                  pack.next(sfn.Choice(self, "Packed?")
                            .when(sfn.Condition.boolean_equals("$.ok", True),
                                  run_task.next(sfn.Succeed(self, "Done")))
                            .otherwise(sfn.Succeed(self, "PackRefused"))))
            .otherwise(sfn.Succeed(self, "NotAdmitted")))

        machine = sfn.StateMachine(
            self, "BoardBrief", state_machine_name=STATE_MACHINE_NAME,
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            state_machine_type=sfn.StateMachineType.STANDARD,
            timeout=Duration.minutes(25))

        self.function, self.state_machine, self.runs_table, self.packs = fn, machine, runs, packs
        self.alarms = self._alarms(fn, machine, alerts_topic_arn)
        CfnOutput(self, "BoardStateMachineArn", value=machine.state_machine_arn)
        CfnOutput(self, "BoardRunsTable", value=runs.table_name)
        CfnOutput(self, "BoardPacksBucket", value=packs.bucket_name)
        CfnOutput(self, "BoardRunnerRoleArn", value=role.role_arn)

    def _alarms(self, fn: lambda_.Function, machine: sfn.StateMachine,
                topic_arn: str) -> list[cw.Alarm]:
        """#329: the board's spend and health alarms, on the main stack's alerts topic (by ARN,
        like every other name this stack imports). OK is sent too (ADR 030 §6)."""
        topic = sns.Topic.from_topic_arn(self, "OncaAlerts", topic_arn)
        day, hour = Duration.days(1), Duration.hours(1)

        def board(name: str) -> cw.Metric:
            return cw.Metric(namespace="Onca/Board", metric_name=name, statistic="Sum", period=day)

        budget = int(HOSTED_DAILY_TOKEN_BUDGET * BUDGET_ALARM_AT)
        slow_ms = int(RUN_TIMEOUT.to_milliseconds() * DURATION_ALARM_AT)
        failures = cw.MathExpression(
            expression="FILL(f, 0) + FILL(t, 0) + FILL(a, 0) + FILL(e, 0)", period=hour,
            using_metrics={"f": machine.metric_failed(period=hour),
                           "t": machine.metric_timed_out(period=hour),
                           "a": machine.metric_aborted(period=hour),
                           "e": fn.metric_errors(period=hour)})
        specs = [
            ("Onca-BoardHostedTokens", board("HostedTokens"), budget,
             f"Board briefs billed to Onça used over {budget:,} tokens in a day "
             f"({BUDGET_ALARM_AT:.0%} of the {HOSTED_DAILY_TOKEN_BUDGET:,} daily budget). Only paid "
             "plans and owner-flagged tenants spend hosted tokens. Look: OncaBoardRuns usage per "
             "run, Logs Insights on /aws/lambda/onca-board-task (HostedTokens by tenant)."),
            ("Onca-BoardRunFailures", failures, 1,
             "A board brief execution failed, timed out or was aborted, or the task Lambda "
             "errored, in the last hour. The run is marked failed for the tenant. Look: Step "
             "Functions onca-board-brief executions, /aws/lambda/onca-board-task."),
            ("Onca-BoardRunDuration", fn.metric_duration(statistic="Maximum", period=hour), slow_ms,
             f"A board brief task ran over {slow_ms // 60000} min, {DURATION_ALARM_AT:.0%} of the "
             f"{RUN_TIMEOUT.to_minutes()}-min Lambda limit. Next: runs get cut off. Look: phase "
             "time boxes in src/board/runner.py, model latency."),
            ("Onca-BoardCappedRuns", board("CappedRuns"), CAPPED_RUNS_PER_DAY,
             f"{CAPPED_RUNS_PER_DAY} or more board briefs hit their token cap in a day and came "
             "back partial. Either the cap is too low for real packs or a prompt loops. Look: "
             "OncaBoardRuns usage vs token_cap, the gaps on those runs."),
        ]
        out = []
        for name, metric, threshold, desc in specs:
            alarm = cw.Alarm(
                self, name.replace("-", ""), alarm_name=name, metric=metric, threshold=threshold,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING, alarm_description=desc)
            alarm.add_alarm_action(cw_actions.SnsAction(topic))
            alarm.add_ok_action(cw_actions.SnsAction(topic))
            out.append(alarm)
        return out
