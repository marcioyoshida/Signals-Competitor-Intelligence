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
            environment={"ONCA_QA_ARTIFACTS_BUCKET": artifacts.bucket_name},
        )

        skeleton_task = sfn_tasks.LambdaInvoke(
            self, "QaSkeletonTask", lambda_function=runner_fn,
            payload=sfn.TaskInput.from_object({}),
        )
        state_machine = sfn.StateMachine(
            self, "QaPipeline", state_machine_name="OncaQaPipeline",
            definition_body=sfn.DefinitionBody.from_chainable(skeleton_task),
            timeout=Duration.minutes(14),
        )
        # Nightly only, not per-PR — cost containment (ADR 027 "Consequences") until real
        # cost data exists. 06:00 UTC = off-peak for this account's operator.
        events.Rule(
            self, "QaNightlyTrigger",
            schedule=events.Schedule.cron(minute="0", hour="6"),
            targets=[targets.SfnStateMachine(state_machine)],
        )
