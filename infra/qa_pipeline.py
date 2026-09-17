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
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
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

        # #128: log in ONCE (the entry QA persona), then fan the resulting sessionStorage
        # out to every {browser, viewport} shard via the Map's item_selector — the state-
        # machine equivalent of Playwright's storageState reuse (ADR 027 "State
        # management"), so 6 shards don't each pay for their own Hosted UI round-trip.
        login_task = sfn_tasks.LambdaInvoke(
            self, "QaLoginTask", lambda_function=runner_fn,
            payload=sfn.TaskInput.from_object({"mode": "login", "persona": "entry"}),
            payload_response_only=True,
        )

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
            }),
            payload_response_only=True,
        )
        matrix_map.item_processor(matrix_task)

        # #129/#130: smoke & routing each do their OWN login(s) internally (checks/smoke.py,
        # checks/routing.py) — they don't need the shared login_task's sessionStorage, so
        # they run as independent Parallel branches alongside the matrix branch rather than
        # sequentially after it.
        smoke_task = sfn_tasks.LambdaInvoke(
            self, "QaSmokeTask", lambda_function=runner_fn,
            payload=sfn.TaskInput.from_object({"mode": "smoke", "persona": "entry"}),
            payload_response_only=True,
        )
        routing_task = sfn_tasks.LambdaInvoke(
            self, "QaRoutingTask", lambda_function=runner_fn,
            payload=sfn.TaskInput.from_object({"mode": "routing", "persona": "entry"}),
            payload_response_only=True,
        )
        # #131: broken-link scan (hard gate) + network interception. Same independent-
        # branch reasoning as smoke/routing — does its own login, no shared state needed.
        resilience_task = sfn_tasks.LambdaInvoke(
            self, "QaResilienceTask", lambda_function=runner_fn,
            payload=sfn.TaskInput.from_object({"mode": "resilience", "persona": "entry"}),
            payload_response_only=True,
        )

        pipeline = sfn.Parallel(self, "QaBranches")
        pipeline.branch(login_task.next(inject_matrix).next(matrix_map))
        pipeline.branch(smoke_task)
        pipeline.branch(routing_task)
        pipeline.branch(resilience_task)

        state_machine = sfn.StateMachine(
            self, "QaPipeline", state_machine_name="OncaQaPipeline",
            definition_body=sfn.DefinitionBody.from_chainable(pipeline),
            timeout=Duration.minutes(14),
        )
        # Nightly only, not per-PR — cost containment (ADR 027 "Consequences") until real
        # cost data exists. 06:00 UTC = off-peak for this account's operator.
        events.Rule(
            self, "QaNightlyTrigger",
            schedule=events.Schedule.cron(minute="0", hour="6"),
            targets=[targets.SfnStateMachine(state_machine)],
        )
