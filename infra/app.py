"""CDK app for the Phase 1.5 Lambda prototype.

This deploys a scheduled Lambda that runs the ingestion prototype and can
later be extended to S3/DynamoDB persistence.
"""
from __future__ import annotations

from pathlib import Path

from aws_cdk import App, Duration, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_lambda as lambda_


REPO_ROOT = Path(__file__).resolve().parents[1]
LAMBDA_ASSET = REPO_ROOT / "build" / "lambda"


class OncaPrototypeStack(Stack):
    def __init__(self, scope: object, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        state_table = dynamodb.Table(
            self,
            "OncaStateTable",
            partition_key=dynamodb.Attribute(name="source", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=None,
        )

        func = lambda_.Function(
            self,
            "OncaLambdaPrototype",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.ingest.lambda_port.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_STATE_TABLE": state_table.table_name,
            },
        )
        state_table.grant_read_write_data(func)

        rule = events.Rule(
            self,
            "OncaDailySchedule",
            schedule=events.Schedule.rate(Duration.days(1)),
            enabled=True,
        )
        rule.add_target(targets.LambdaFunction(func))


app = App()
OncaPrototypeStack(app, "OncaPrototypeStack")
app.synth()
