"""AWS-native GitOps CI/CD for the CDK/CloudFormation app (issue #6).

Replaces the GitHub-hosted-runner Actions workflow with an in-account
**CodePipeline + CodeBuild** pipeline (CodeDeploy targets EC2/Lambda/ECS app
revisions, not CFN stacks, so it is not the fit for a CDK stack):

    GitHub (main)  ──CodeStar Connection──▶  CodePipeline
                                              └─ Deploy stage: CodeBuild runs
                                                 buildspec.yml → pytest → stage
                                                 build/lambda → `cdk deploy
                                                 OncaPrototypeStack`

Deployed as its OWN stack (``OncaCicdStack``) so the pipeline that deploys the app
stack is not part of the app stack (no self-mutation / chicken-and-egg). The
CodeBuild role is least-privilege for CDK: it only assumes the CDK **bootstrap**
roles (which hold the real deploy permissions) plus reads the bootstrap version.

One-time activation: after ``cdk deploy OncaCicdStack``, authorize the GitHub
connection in the console (Developer Tools → Settings → Connections) — it starts
PENDING. See docs/2026-08-25-cicd-codebuild.md.
"""
from __future__ import annotations

from aws_cdk import Stack
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_codepipeline_actions as cp_actions
from aws_cdk import aws_codestarconnections as codestar
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_OWNER = "marcioyoshida"
GITHUB_REPO = "Signals-Competitor-Intelligence"
GITHUB_BRANCH = "main"
CDK_QUALIFIER = "hnb659fds"  # default cdk bootstrap qualifier


class OncaCicdStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # GitHub v2 (CodeStar) connection — created PENDING; authorized once in the
        # console. Owns the OAuth handshake so no PAT/secret lives in the account.
        connection = codestar.CfnConnection(
            self, "GithubConnection",
            connection_name="onca-github",
            provider_type="GitHub",
        )

        # CodeBuild project: tests + stage the Lambda asset + `cdk deploy`. Steps
        # live in the repo's buildspec.yml (GitOps — the build is versioned too).
        project = codebuild.PipelineProject(
            self, "DeployProject",
            project_name="onca-cicd-deploy",
            build_spec=codebuild.BuildSpec.from_source_filename("buildspec.yml"),
            environment=codebuild.BuildEnvironment(
                # STANDARD_7_0 = Ubuntu 22.04 with Python 3.11 + Node 20 + rsync.
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                compute_type=codebuild.ComputeType.SMALL,
            ),
        )
        # Least-privilege CDK deploy: assume the bootstrap roles + read the
        # bootstrap version + describe stacks. The bootstrap roles carry the real
        # permissions, so the build role stays narrow.
        acct, region = self.account, self.region
        project.add_to_role_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=[f"arn:aws:iam::{acct}:role/cdk-{CDK_QUALIFIER}-*-{acct}-{region}"],
        ))
        project.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudformation:DescribeStacks"], resources=["*"],
        ))
        project.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters"],
            resources=[f"arn:aws:ssm:{region}:{acct}:parameter/cdk-bootstrap/{CDK_QUALIFIER}/version"],
        ))

        source_output = codepipeline.Artifact()
        source_action = cp_actions.CodeStarConnectionsSourceAction(
            action_name="GitHub_main",
            owner=GITHUB_OWNER,
            repo=GITHUB_REPO,
            branch=GITHUB_BRANCH,
            connection_arn=connection.attr_connection_arn,
            output=source_output,
            trigger_on_push=True,
        )
        deploy_action = cp_actions.CodeBuildAction(
            action_name="TestAndDeploy",
            project=project,
            input=source_output,
        )
        codepipeline.Pipeline(
            self, "OncaCicd",
            pipeline_name="onca-cicd",
            restart_execution_on_update=False,
            stages=[
                codepipeline.StageProps(stage_name="Source", actions=[source_action]),
                codepipeline.StageProps(stage_name="Deploy", actions=[deploy_action]),
            ],
        )
