"""ADR 027 — OncaQaPipeline: Playwright + Bedrock navigation/visual QA, own stack (#127).

Deployed as its OWN stack, separate from OncaPrototypeStack — its own IAM role, its own
failure domain, must never couple to (or block) the data pipeline or the app deploy. See
docs/2026-09-16-adr027-e2e-navigation-visual-qa-pipeline.md for the full design.

Two-phase because no box that runs `cdk deploy` for this repo has a usable local Docker
daemon (confirmed: WSL without the Docker Desktop integration active) — the Lambda container
image can ONLY be built by CodeBuild (privileged mode, docker-in-docker), so building/pushing
the image is a SEPARATE resource (`OncaQaImageBuild`) from the Lambda that consumes it:

    Phase A (always deployed): ECR repo + S3 artifacts bucket + the CodeBuild image-build
    project. A first `cdk deploy OncaQaPipelineStack` creates these — no image required yet.

    Phase B (gated on `-c qa_pipeline_image_tag=...`, the SAME durable-context pattern as
    Google OAuth's `google_client_id` — see docs/google-oauth-runbook.md gotcha #4, where an
    un-persisted `-c` flag got silently dropped by an unrelated deploy): the container Lambda
    + the `OncaQaPipeline` state machine + a nightly EventBridge trigger. Only synthesizes
    once an image with that tag actually exists in ECR — CloudFormation validates the image
    URI when creating the Lambda, so deploying Phase B before the image exists fails outright.
    Once confirmed working, persist the tag in cdk.json rather than leaving it ad hoc.

One-time manual step after the first `cdk deploy OncaQaPipelineStack` (no source-control
trigger wired yet — auto-triggering this on `qa_pipeline/**` pushes is a documented,
not-yet-done follow-up, see qa_pipeline/README.md):

    zip -r /tmp/qa-image-source.zip . -x '.venv/*' -x 'node_modules/*' -x '.git/*' -x 'cdk.out/*'
    aws s3 cp /tmp/qa-image-source.zip s3://<QaArtifacts bucket>/_source/qa-image-source.zip
    aws codebuild start-build --project-name onca-qa-image-build
    # wait for the build to reach SUCCEEDED, then:
    cd infra && cdk deploy OncaQaPipelineStack -c qa_pipeline_image_tag=latest
"""
from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Size, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as sns_subs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as sfn_tasks
from constructs import Construct


class OncaQaPipelineStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Phase A: artifacts bucket + ECR repo + the image-build CodeBuild project -----
        # Debug output (screenshots/video/trace/HTML report), not a data asset — short
        # lifecycle. `auto_delete_objects` + DESTROY is deliberate: this bucket holding old
        # QA runs must never block a stack teardown the way a data table's RETAIN would.
        artifacts = s3.Bucket(
            self, "QaArtifacts",
            bucket_name=f"onca-qa-artifacts-{self.account}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))],
        )

        repo = ecr.Repository(
            self, "QaRunnerRepo",
            repository_name="onca-qa-runner",
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
            image_scan_on_push=True,
        )

        image_build = codebuild.Project(
            self, "QaImageBuild",
            project_name="onca-qa-image-build",
            source=codebuild.Source.s3(bucket=artifacts, path="_source/qa-image-source.zip"),
            build_spec=codebuild.BuildSpec.from_source_filename("qa_pipeline/buildspec-image.yml"),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                compute_type=codebuild.ComputeType.MEDIUM,
                privileged=True,  # docker build/push needs docker-in-docker
            ),
            environment_variables={
                "ECR_REPO_URI": codebuild.BuildEnvironmentVariable(value=repo.repository_uri),
            },
            timeout=Duration.minutes(20),
        )
        repo.grant_pull_push(image_build)

        # --- Phase B: the container Lambda + state machine + nightly trigger --------------
        image_tag = self.node.try_get_context("qa_pipeline_image_tag")
        if not image_tag:
            return

        fn_role = iam.Role(
            self, "QaRunnerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"),
            ],
        )
        artifacts.grant_read_write(fn_role)
        # Least-privilege reads: the ONE secret + the TWO SSM params this Lambda needs,
        # nothing else in the account's Secrets Manager/Parameter Store.
        fn_role.add_to_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[
                f"arn:aws:secretsmanager:{self.region}:{self.account}:"
                "secret:signalscompetitor/onca/qa-test-credentials-*"
            ],
        ))
        fn_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                f"arn:aws:ssm:{self.region}:{self.account}:parameter/onca/dashboard/basic-auth-user",
                f"arn:aws:ssm:{self.region}:{self.account}:parameter/onca/dashboard/basic-auth-pass",
                # #130: the routing checks exercise the ?admin=1&opkey=... operator bypass
                # (#122), which needs this second, narrower secret on top of basic auth.
                f"arn:aws:ssm:{self.region}:{self.account}:parameter/onca/dashboard/operator-secret",
            ],
        ))
        # #132: Bedrock (Nova Pro) vision calls — same action pair + resource-scoping
        # pattern already used elsewhere in this account (infra/app.py), not a new pattern.
        fn_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:Converse"],
            resources=[
                f"arn:aws:bedrock:{self.region}::foundation-model/*",
                f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
            ],
        ))

        runner_fn = lambda_.DockerImageFunction(
            self, "QaRunnerFn",
            function_name="onca-qa-runner",
            code=lambda_.DockerImageCode.from_ecr(repo, tag_or_digest=image_tag),
            role=fn_role,
            architecture=lambda_.Architecture.X86_64,
            memory_size=2048,
            timeout=Duration.minutes(10),
            ephemeral_storage_size=Size.mebibytes(2048),
            environment={
                "ONCA_QA_ARTIFACTS_BUCKET": artifacts.bucket_name,
                # Confirmed live (#128): Firefox hangs for its full 180s launch timeout
                # trying to write its profile/cache under $HOME, which Lambda's execution
                # environment leaves read-only ("unable to create directory
                # '/home/sbx_user.../.cache/dconf': Read-only file system"). Chromium
                # tolerates this; Firefox (and, empirically, WebKit) do not. /tmp is the
                # one writable path in a Lambda container.
                "HOME": "/tmp",
                "XDG_CACHE_HOME": "/tmp/.cache",
                "XDG_CONFIG_HOME": "/tmp/.config",
            },
        )

        # #133: every task carries the SAME run_id (the state machine execution's own name)
        # so every branch's artifacts land under one shared S3 prefix the report (#133) can
        # correlate — before this, each Lambda invocation minted its OWN timestamp, so one
        # execution's matrix/smoke/routing/resilience/vision artifacts were scattered across
        # several different run_id prefixes.
        RUN_ID = {"run_id.$": "$$.Execution.Name"}

        # #128: log in ONCE (the entry QA persona), then fan the resulting sessionStorage
        # out to every {browser, viewport} shard via the Map's item_selector — the state-
        # machine equivalent of Playwright's storageState reuse (ADR 027 "State
        # management"), so 6 shards don't each pay for their own Hosted UI round-trip.
        login_task = sfn_tasks.LambdaInvoke(
            self, "QaLoginTask", lambda_function=runner_fn,
            payload=sfn.TaskInput.from_object({"mode": "login", "persona": "entry", **RUN_ID}),
            payload_response_only=True,
        )
        # #133: a hard-gate failure must not make the WHOLE run invisible to the report —
        # catch it into $.error so this branch's chain still completes (with an
        # error-shaped output instead of a crash), letting QaReportTask always run. Each
        # check module already uploads its full result JSON to S3 before raising
        # (see e.g. checks/smoke.py), so the report can still recover full detail on a
        # failure via its S3 fallback read, not just this summary error.
        login_task.add_catch(sfn.Pass(self, "QaLoginFailed"), result_path="$.error")

        # WebKit is deliberately NOT in this matrix yet: it crashes on `new_page()` inside
        # the Lambda execution environment with no diagnostic stderr at all (unlike
        # Chromium/Firefox, both of which crashed loudly and were fixed — see
        # CHROMIUM_LAUNCH_ARGS in handler.py and the HOME/XDG env vars above). Tried and
        # ruled out: WEBKIT_DISABLE_COMPOSITING_MODE=1 (a documented WebKit-in-Docker
        # fix elsewhere) made no difference. Documented as a known gap rather than an
        # open-ended debugging spiral — see qa_pipeline/README.md.
        MATRIX = [
            {"browser": browser, "viewport": viewport}
            for browser in ("chromium", "firefox")
            for viewport in ("desktop", "tablet", "phone")
        ]
        # `sfn.Map`'s inline `items=` prop is JSONata-only; this state machine (like the
        # rest of this repo's Step Functions definitions) uses classic JSONPath, so the
        # fixed matrix has to be injected into the state's JSON via a Pass first, then
        # referenced by `items_path`.
        inject_matrix = sfn.Pass(
            self, "QaMatrixSpec",
            # Result.from_object requires a Mapping (jsii type-checked), hence the extra
            # {"list": [...]} nesting rather than injecting the array directly.
            result=sfn.Result.from_object({"list": MATRIX}), result_path="$.matrix",
        )
        matrix_map = sfn.Map(
            self, "QaMatrix",
            items_path="$.matrix.list",
            item_selector={
                "browser.$": "$$.Map.Item.Value.browser",
                "viewport.$": "$$.Map.Item.Value.viewport",
                "session_storage.$": "$.session_storage",
                "run_id.$": "$.run_id",
            },
            # Cost/quota containment (ADR 027 "Cross-browser & responsive") — bounded
            # parallelism, not unbounded fan-out.
            max_concurrency=3,
        )
        matrix_task = sfn_tasks.LambdaInvoke(
            self, "QaMatrixShard", lambda_function=runner_fn,
            payload=sfn.TaskInput.from_object({
                "mode": "matrix",
                "browser.$": "$.browser",
                "viewport.$": "$.viewport",
                "session_storage.$": "$.session_storage",
                "run_id.$": "$.run_id",
            }),
            payload_response_only=True,
        )
        # #133: one bad shard must not blank out the other 5 — catch into an error-shaped
        # item instead of failing the whole Map (Step Functions' default Map behavior is to
        # abort entirely on any iteration's failure).
        matrix_task.add_catch(
            sfn.Pass(self, "QaMatrixShardFailed"), result_path="$.error")
        matrix_map.item_processor(matrix_task)

        # #129/#130: smoke & routing each do their OWN login(s) internally (checks/smoke.py,
        # checks/routing.py) — they don't need the shared login_task's sessionStorage, so
        # they run as independent Parallel branches alongside the matrix branch rather than
        # sequentially after it.
        smoke_task = sfn_tasks.LambdaInvoke(
            self, "QaSmokeTask", lambda_function=runner_fn,
            payload=sfn.TaskInput.from_object({"mode": "smoke", "persona": "entry", **RUN_ID}),
            payload_response_only=True,
        )
        smoke_task.add_catch(sfn.Pass(self, "QaSmokeFailed"), result_path="$.error")

        routing_task = sfn_tasks.LambdaInvoke(
            self, "QaRoutingTask", lambda_function=runner_fn,
            payload=sfn.TaskInput.from_object({"mode": "routing", "persona": "entry", **RUN_ID}),
            payload_response_only=True,
        )
        routing_task.add_catch(sfn.Pass(self, "QaRoutingFailed"), result_path="$.error")

        # #131: broken-link scan (hard gate) + network interception. Same independent-
        # branch reasoning as smoke/routing — does its own login, no shared state needed.
        resilience_task = sfn_tasks.LambdaInvoke(
            self, "QaResilienceTask", lambda_function=runner_fn,
            payload=sfn.TaskInput.from_object({"mode": "resilience", "persona": "entry", **RUN_ID}),
            payload_response_only=True,
        )
        resilience_task.add_catch(sfn.Pass(self, "QaResilienceFailed"), result_path="$.error")

        # #132: Bedrock (Nova Pro) visual QA — advisory, checks/vision.py never raises on
        # its own, but this catch is cheap insurance against a truly unexpected crash (e.g.
        # an import error) still letting the report run.
        vision_task = sfn_tasks.LambdaInvoke(
            self, "QaVisionTask", lambda_function=runner_fn,
            payload=sfn.TaskInput.from_object({"mode": "vision", "persona": "entry", **RUN_ID}),
            payload_response_only=True,
        )
        vision_task.add_catch(sfn.Pass(self, "QaVisionFailed"), result_path="$.error")

        pipeline = sfn.Parallel(self, "QaBranches")
        pipeline.branch(login_task.next(inject_matrix).next(matrix_map))
        pipeline.branch(smoke_task)
        pipeline.branch(routing_task)
        pipeline.branch(resilience_task)
        pipeline.branch(vision_task)

        # #133: consolidate every branch's output into one static HTML report. A Pass
        # reshapes the Parallel's raw array output (branch order = declaration order above)
        # into the {mode, run_id, branches} shape checks/report.py expects.
        inject_report_input = sfn.Pass(
            self, "QaReportInput",
            parameters={"mode": "report", "run_id.$": "$$.Execution.Name", "branches.$": "$"},
        )
        report_task = sfn_tasks.LambdaInvoke(
            self, "QaReportTask", lambda_function=runner_fn,
            payload=sfn.TaskInput.from_object({
                "mode": "report", "run_id.$": "$.run_id", "branches.$": "$.branches",
            }),
            payload_response_only=True,
        )

        state_machine = sfn.StateMachine(
            self, "QaPipeline", state_machine_name="OncaQaPipeline",
            definition_body=sfn.DefinitionBody.from_chainable(
                pipeline.next(inject_report_input).next(report_task)),
            timeout=Duration.minutes(14),
        )
        # Nightly only, not per-PR — cost containment (ADR 027 "Consequences") until real
        # cost data exists. 06:00 UTC = off-peak for this account's operator.
        events.Rule(
            self, "QaNightlyTrigger",
            schedule=events.Schedule.cron(minute="0", hour="6"),
            targets=[targets.SfnStateMachine(state_machine)],
        )

        # Failure notification. Own SNS topic — deliberately NOT the main stack's
        # OncaAlertsTopic/alert_fn (SES-based, formatted for the CSO digest channel):
        # OncaQaPipelineStack must stay its own failure domain (ADR 027), and a plain SNS
        # email subscription is the whole notification need here, no Lambda in between.
        # `alertEmail` follows the SAME durable-context pattern as `google_client_id`/
        # `qa_pipeline_image_tag` — a bare `-c` flag with nothing wired into cdk.json would
        # be exactly the kind of un-persisted context an unrelated deploy could silently
        # drop (see docs/google-oauth-runbook.md gotcha #4).
        alerts_topic = sns.Topic(self, "QaAlertsTopic", topic_name="onca-qa-pipeline-alerts")
        alert_email = self.node.try_get_context("alertEmail")
        if alert_email:
            alerts_topic.add_subscription(sns_subs.EmailSubscription(alert_email))
        # Mirrors OncaPrototypeStack's OncaPipelineFailedAlarm/OncaPipelineTimedOutAlarm
        # shape exactly (infra/app.py) — nightly cadence, `evaluation_periods=1` over a
        # 1-day period means ANY failed/timed-out execution in a day fires once, no
        # flapping risk to smooth out. A hard-gate failure reaches this metric via
        # QaReportTask's deliberate raise (see checks/report.py's module docstring) —
        # every branch upstream is .add_catch()'d specifically so the report still runs,
        # which would otherwise leave the execution showing SUCCEEDED.
        cloudwatch.Alarm(
            self, "QaPipelineFailedAlarm",
            metric=state_machine.metric_failed(period=Duration.days(1), statistic="Sum"),
            threshold=1, evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(alerts_topic))
        cloudwatch.Alarm(
            self, "QaPipelineTimedOutAlarm",
            metric=state_machine.metric_timed_out(period=Duration.days(1), statistic="Sum"),
            threshold=1, evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(alerts_topic))
