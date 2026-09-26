"""O1 (#136): cross-cloud AWS→GCP credential bridge for the GDELT macro-theme spike.

Own stack, deliberately isolated from ``OncaPrototypeStack`` — same reasoning as
``qa_pipeline.py``: this Lambda's only job is to prove an AWS execution role can reach
BigQuery via GCP workload identity federation, and it has no business sharing a deploy
unit, an IAM role, or a Lambda code asset with the ~80 functions in the main stack. See
``src/ingest/gdelt_bridge.py`` for the handler and the credential-flow rationale.

Two-phase, the same durable-context pattern as ``qa_pipeline.py``'s image-tag gate:

    Phase A (always deployed): the Lambda + its own IAM role with a FIXED, predictable
    name (``OncaGdeltBridgeRole``) — CDK's default auto-generated names include a random
    hash suffix, and the GCP-side workload identity provider has to trust one exact role
    ARN, decided *before* GCP is configured. Ships with a stub-safe handler: with no
    ``GOOGLE_APPLICATION_CREDENTIALS`` configured yet, it returns a clear "not configured"
    result rather than crashing, so this phase is safe to deploy before GCP exists.

    Phase B (gated on ``-c gdelt_bridge_ready=1``): once the GCP-side workload identity
    pool/provider/service-account trust is provisioned (manual, GCP console/gcloud — see
    the O1 issue and ``Signals-GDELT-Spine`` docs) and the resulting ``external_account``
    JSON config is generated, that config + the google-auth/google-cloud-bigquery deps
    get bundled into ``build/lambda-gdelt-bridge`` and the Lambda picks up real env vars.

One-time build step (mirrors the main stack's ``build/lambda`` staging, but isolated —
see ``gdelt_bridge_requirements.txt`` for why this can't share the main asset):

    rm -rf build/lambda-gdelt-bridge && mkdir -p build/lambda-gdelt-bridge
    pip install -r infra/gdelt_bridge_requirements.txt -t build/lambda-gdelt-bridge
    cp src/ingest/gdelt_bridge.py src/ingest/gdelt_macro.py src/ingest/gdelt_macro_handler.py \
      build/lambda-gdelt-bridge/
    # Phase B only: cp <path-to-generated-cred-config>.json build/lambda-gdelt-bridge/gcp_cred_config.json

O2 (#137) adds a second Lambda (``GdeltMacroFn``) in this SAME stack, sharing the SAME
IAM role — not a new role — because the GCP-side workload identity provider trusts one
exact role name (``OncaGdeltBridgeRole``); giving O2 its own role would mean redoing the
whole GCP trust setup for no benefit, since both functions have the identical "reach
BigQuery, nothing else" job. The role picks up one incremental permission: S3 PutObject
scoped to the ``lambda-digests/gdelt_macro/*`` prefix of the existing shared digests
bucket (imported by name, not created — same account-level bucket ``lambda_port.py``
already writes ``lambda-digests/news/*`` into).
"""
from __future__ import annotations

from pathlib import Path

from aws_cdk import Duration, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[1]
GDELT_BRIDGE_ASSET = REPO_ROOT / "build" / "lambda-gdelt-bridge"
# #104 — its own isolated asset: the shared build/lambda src/ tree (needed for
# `receita_bulk.propose_candidates` -> `entity_registry`) PLUS the same heavy
# google-cloud-bigquery deps as GDELT's asset. Kept separate from GDELT_BRIDGE_ASSET
# so a change to one function's deps never risks the other's build.
RECEITA_BIGQUERY_ASSET = REPO_ROOT / "build" / "lambda-receita-bigquery"

ROLE_NAME = "OncaGdeltBridgeRole"
FN_NAME = "OncaGdeltBridgeFn"
MACRO_FN_NAME = "OncaGdeltMacroFn"
RECEITA_FN_NAME = "OncaReceitaBigqueryFn"


class OncaGdeltBridgeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, entities_table=None, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        gcp_ready = self.node.try_get_context("gdelt_bridge_ready")

        # Fixed name: the GCP workload identity provider's trust condition pins to this
        # exact role ARN, decided before GCP exists — CDK's default naming (a random hash
        # suffix) would make that a chicken-and-egg problem.
        role = iam.Role(
            self,
            "GdeltBridgeRole",
            role_name=ROLE_NAME,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            description="O1 (#136): AWS side of the GCP workload-identity-federation "
            "trust for the GDELT macro-theme spike. No AWS resource access beyond "
            "its own logs; this function's only job is to reach BigQuery over GCP's "
            "own STS, not to touch anything in this account.",
        )

        env = {}
        if gcp_ready:
            env["GOOGLE_APPLICATION_CREDENTIALS"] = "/var/task/gcp_cred_config.json"
            env["GCP_PROJECT"] = str(self.node.try_get_context("gcp_project") or "onssa-508623")

        lambda_.Function(
            self,
            "GdeltBridgeFn",
            function_name=FN_NAME,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="gdelt_bridge.handler",
            code=lambda_.Code.from_asset(str(GDELT_BRIDGE_ASSET)),
            role=role,
            timeout=Duration.seconds(30),
            memory_size=256,
            environment=env,
        )

        # O2 (#137) — imported, not created: the same account-level digests bucket
        # `lambda_port.py` already writes `lambda-digests/news/*` into.
        digests_bucket = s3.Bucket.from_bucket_name(
            self, "GdeltMacroDigestsBucket", f"onca-digests-{self.account}"
        )
        digests_bucket.grant_put(role, "lambda-digests/gdelt_macro/*")

        macro_env = dict(env)
        macro_env["ONCA_DIGESTS_BUCKET"] = digests_bucket.bucket_name

        macro_fn = lambda_.Function(
            self,
            "GdeltMacroFn",
            function_name=MACRO_FN_NAME,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="gdelt_macro_handler.handler",
            code=lambda_.Code.from_asset(str(GDELT_BRIDGE_ASSET)),
            role=role,
            timeout=Duration.minutes(2),
            memory_size=256,
            environment=macro_env,
        )

        if gcp_ready:
            # Daily, well after GKG's own data for "yesterday" is complete (GKG updates
            # every 15 min but this Lambda always targets `today - 1` — see
            # gdelt_macro_handler.py — so any time after midnight UTC is safe; picked
            # 07:00 UTC to land alongside the existing nightly QA pipeline cadence
            # rather than clustering everything at 00:00).
            events.Rule(
                self,
                "GdeltMacroDailyTrigger",
                schedule=events.Schedule.cron(minute="0", hour="7"),
                targets=[targets.LambdaFunction(macro_fn)],
                description="O2 (#137): daily GDELT macro-theme sweep, ~$0.002/run.",
            )

        # #104 (#14 Stage 2) — Receita CNAE discovery via the public basedosdados
        # BigQuery mirror, replacing the live-shard-streaming approach that measured
        # non-viable on a shared Lambda (885s against a 900s ceiling). SAME role as
        # the GDELT functions above — reusing it means zero new GCP-side trust/grant
        # work, since the GCP workload identity provider trusts this one AWS role by
        # name, not by function. `entities_table` is a real cross-stack CDK token from
        # `OncaPrototypeStack` (see infra/app.py) — the registry this function proposes
        # into, not a table this stack owns.
        receita_env = dict(env)
        if entities_table is not None:
            receita_env["ONCA_ENTITIES_TABLE"] = entities_table.table_name
        receita_env["PYTHONPATH"] = "/var/task"
        # Throttled for the SAME reason the retired shard-based path was (see
        # infra/app.py's ONCA_INGEST_RECEITA_BULK comment): every proposal lands
        # in a human curator's review queue — 25/day keeps discovery flowing
        # without a flood. The code default (200) stays available for a manual
        # ad-hoc invoke that deliberately wants to seed/clear a larger backlog.
        receita_env["ONCA_RECEITA_BQ_MAX_PROPOSE"] = "25"

        receita_fn = lambda_.Function(
            self,
            "ReceitaBigqueryFn",
            function_name=RECEITA_FN_NAME,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="receita_bigquery_handler.handler",
            code=lambda_.Code.from_asset(str(RECEITA_BIGQUERY_ASSET)),
            role=role,
            timeout=Duration.minutes(5),
            memory_size=512,
            environment=receita_env,
        )
        if entities_table is not None:
            # propose_candidates only ever proposes (ADR 011 §4) — never auto-creates
            # — but load_cnpj_root_map + propose_review both need read+write on the
            # registry table itself, same grant every other discovery-writing Lambda
            # in the main stack already has.
            entities_table.grant_read_write_data(role)
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:PutParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/onca/receita-bq/*"],
        ))

        if gcp_ready:
            # Monthly (the mirror refreshes monthly at best); the handler additionally
            # skips the ~7 GiB scan when the snapshot hasn't moved since the last run
            # (SSM marker). 08:00 UTC lands after the GDELT sweep.
            events.Rule(
                self,
                "ReceitaBigqueryMonthlyTrigger",
                schedule=events.Schedule.cron(minute="0", hour="8", day="5"),
                targets=[targets.LambdaFunction(
                    receita_fn, event=events.RuleTargetInput.from_object({"mode": "discover"})
                )],
                description="#104: monthly Receita CNAE discovery via BigQuery, snapshot-gated, propose-only.",
            )
