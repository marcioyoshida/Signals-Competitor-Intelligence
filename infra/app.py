"""CDK app for the Phase 1.5 Lambda prototype.

This deploys a scheduled Lambda that runs the ingestion prototype and can
later be extended to S3/DynamoDB persistence.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

import yaml
from aws_cdk import App, CfnOutput, Duration, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as cf_origins
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_s3vectors as s3vectors
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as sfn_tasks

EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
VECTOR_DIMENSION = 1024


REPO_ROOT = Path(__file__).resolve().parents[1]
LAMBDA_ASSET = REPO_ROOT / "build" / "lambda"
SITE_ASSET = REPO_ROOT / "src" / "dashboard" / "site"
WATCHLIST_CONFIG = REPO_ROOT / "config" / "watchlist.yaml"

# Dashboard basic-auth creds live in SSM (provisioned out-of-band by
# bootstrap.sh) so they survive a plain `cdk deploy` — otherwise a deploy with
# no ONCA_DASH_* env vars silently resets the password to the default.
DASH_USER_PARAM = "/onca/dashboard/basic-auth-user"
DASH_PASS_PARAM = "/onca/dashboard/basic-auth-pass"


def dashboard_credentials() -> tuple[str, str]:
    """Resolve dashboard basic-auth (user, pass) at synth time.

    Precedence per field: ONCA_DASH_* env override -> SSM Parameter Store ->
    built-in default. The CloudFront Function needs the literal string baked in,
    so we read the SecureString value directly via boto3 using the deployer's
    credentials (no stack IAM needed); it already lives base64'd in the deployed
    function, so SSM is just the durable source of truth.
    """
    user = os.environ.get("ONCA_DASH_USER")
    pw = os.environ.get("ONCA_DASH_PASS")
    if not (user and pw):
        try:
            import boto3

            ssm = boto3.client("ssm")
            if not user:
                user = ssm.get_parameter(Name=DASH_USER_PARAM)["Parameter"]["Value"]
            if not pw:
                pw = ssm.get_parameter(Name=DASH_PASS_PARAM, WithDecryption=True)[
                    "Parameter"
                ]["Value"]
        except Exception as exc:  # param missing / no creds — fall back below
            print(f"WARNING: dashboard creds not read from SSM ({DASH_PASS_PARAM}): {exc}")
    user = user or "onca"
    if not pw:
        pw = "warroom"  # prototype default — set the SSM param before real use
        print("WARNING: dashboard password not in env or SSM; using default 'warroom'")
    return user, pw


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

        # Entities registry (ADR 2026-08-17): single-table lookup by typed pk
        # (ENT#/ALIAS#/CNPJ#). Seeded from ENTITY_ALIASES; self-expands later.
        entities_table = dynamodb.Table(
            self,
            "OncaEntitiesTable",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=None,
        )

        # Provisioned out-of-band by infra/bootstrap.sh (account-level baseline
        # buckets, shared across future stacks) — import them, don't create them.
        digests_bucket = s3.Bucket.from_bucket_name(
            self,
            "OncaDigestsBucket",
            f"onca-digests-{self.account}",
        )
        raw_bucket = s3.Bucket.from_bucket_name(
            self,
            "OncaRawBucket",
            f"onca-raw-{self.account}",
        )

        # Phase 2 Stage A: Bedrock Knowledge Base backed by S3 Vectors (not
        # OpenSearch Serverless — see CLAUDE.md). Feeds correlation logic
        # (Stage B, not yet built) with a queryable, citable document corpus.
        vector_bucket = s3vectors.CfnVectorBucket(
            self,
            "OncaVectorBucket",
            vector_bucket_name=f"onca-vectors-{self.account}",
        )
        vector_index = s3vectors.CfnIndex(
            self,
            "OncaVectorIndex",
            vector_bucket_name=vector_bucket.vector_bucket_name,
            index_name="onca-corpus",
            data_type="float32",
            dimension=VECTOR_DIMENSION,
            distance_metric="cosine",
        )
        vector_index.add_dependency(vector_bucket)

        kb_role = iam.Role(
            self,
            "OncaKnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
        )
        raw_bucket.grant_read(kb_role)
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL_ID}"],
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3vectors:*"],
                resources=[vector_bucket.attr_vector_bucket_arn, vector_index.attr_index_arn],
            )
        )

        knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "OncaKnowledgeBase",
            name="onca-corpus",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL_ID}",
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    vector_bucket_arn=vector_bucket.attr_vector_bucket_arn,
                    index_arn=vector_index.attr_index_arn,
                ),
            ),
        )
        knowledge_base.add_dependency(vector_index)
        knowledge_base.node.add_dependency(kb_role)

        data_source = bedrock.CfnDataSource(
            self,
            "OncaKnowledgeBaseDataSource",
            knowledge_base_id=knowledge_base.attr_knowledge_base_id,
            name="onca-raw-corpus",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=raw_bucket.bucket_arn,
                ),
            ),
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy="FIXED_SIZE",
                    fixed_size_chunking_configuration=bedrock.CfnDataSource.FixedSizeChunkingConfigurationProperty(
                        max_tokens=300,
                        overlap_percentage=10,
                    ),
                ),
            ),
        )

        watchlist = yaml.safe_load(WATCHLIST_CONFIG.read_text())

        func = lambda_.Function(
            self,
            "OncaLambdaPrototype",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.ingest.lambda_port.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            # Ingest does many sequential live fetches (BCB/CVM/SEC/IF.data,
            # rate-limited); 5 min was too tight and timed out. Give it the
            # Lambda max headroom so the pipeline's ingest step completes.
            timeout=Duration.minutes(15),
            memory_size=1024,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_STATE_TABLE": state_table.table_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_LOOKBACK_DAYS": str(watchlist.get("lookback_days", 7)),
                "ONCA_COMPETITORS": ",".join(watchlist.get("competitors", [])),
                # Pix: empty ISPB list = rank all institutions (noisier).
                "ONCA_COMPETITOR_ISPB": ",".join(
                    str(x) for x in (watchlist.get("competitor_ispb") or [])
                ),
                "ONCA_PIX_MOVE_THRESHOLD_PCT": str(
                    watchlist.get("pix_move_threshold_pct", 15.0)
                ),
                # Juros médios: empty competitors = all institutions; empty
                # modalities + use_defaults=true → DEFAULT_MODALITY_FILTERS in code.
                "ONCA_JUROS_COMPETITORS": ",".join(
                    str(x) for x in (watchlist.get("juros_competitors") or [])
                ),
                "ONCA_JUROS_MODALITIES": ",".join(
                    str(x) for x in (watchlist.get("juros_modalities") or [])
                ),
                "ONCA_JUROS_USE_DEFAULT_MODALITIES": (
                    "true" if watchlist.get("juros_use_default_modalities", True) else "false"
                ),
                "ONCA_JUROS_MOVE_THRESHOLD_PCT": str(
                    watchlist.get("juros_move_threshold_pct", 10.0)
                ),
                "ONCA_OFERTAS_LOOKBACK_DAYS": str(
                    watchlist.get("ofertas_lookback_days", 30)
                ),
                "ONCA_OFERTAS_WATCHLIST": ",".join(
                    str(x) for x in (watchlist.get("ofertas_watchlist") or [])
                ),
                "ONCA_OFERTAS_USE_COMPETITORS": (
                    "true"
                    if watchlist.get("ofertas_use_competitors_watchlist", True)
                    else "false"
                ),
                # CVM material facts (Fato Relevante / Comunicado ao Mercado).
                "ONCA_FATOS_LOOKBACK_DAYS": str(watchlist.get("fatos_lookback_days", 45)),
                "ONCA_FATOS_WATCHLIST": ",".join(
                    str(x) for x in (watchlist.get("fatos_watchlist") or [])
                ),
                "ONCA_FATOS_USE_COMPETITORS": (
                    "true" if watchlist.get("fatos_use_competitors", True) else "false"
                ),
                "ONCA_FATOS_CATEGORIES": ",".join(
                    str(x) for x in (watchlist.get("fatos_categories") or [])
                ),
                # Diário Oficial (SUSEP / CADE / BACEN acts naming a competitor).
                "ONCA_DOU_LOOKBACK_DAYS": str(watchlist.get("dou_lookback_days", 30)),
                "ONCA_DOU_WATCHLIST": ",".join(
                    str(x) for x in (watchlist.get("dou_watchlist") or [])
                ),
                "ONCA_DOU_USE_COMPETITORS": (
                    "true" if watchlist.get("dou_use_competitors", True) else "false"
                ),
                # Trade-press (Google News RSS) — brand-name queries.
                "ONCA_NEWS_LOOKBACK_DAYS": str(watchlist.get("news_lookback_days", 14)),
                "ONCA_NEWS_WATCHLIST": ",".join(
                    str(x) for x in (watchlist.get("news_watchlist") or [])
                ),
                "ONCA_NEWS_USE_COMPETITORS": (
                    "true" if watchlist.get("news_use_competitors", True) else "false"
                ),
                # SEC EDGAR — payments/US-listed fintechs; empty tickers = skip.
                "ONCA_SEC_TICKERS": ",".join(
                    str(x) for x in (watchlist.get("sec_tickers") or [])
                ),
                "ONCA_SEC_LOOKBACK_DAYS": str(
                    watchlist.get("sec_lookback_days", 365)
                ),
                "ONCA_SEC_USER_AGENT": str(
                    watchlist.get("sec_user_agent")
                    or "Onca Competitive Intelligence marcioyoshida@gmail.com"
                ),
                "ONCA_INF_DIARIO_WATCHLIST": ",".join(
                    str(x) for x in (watchlist.get("inf_diario_watchlist") or [])
                ),
                "ONCA_INF_DIARIO_USE_COMPETITORS": (
                    "true"
                    if watchlist.get("inf_diario_use_competitors_watchlist", True)
                    else "false"
                ),
                "ONCA_INF_DIARIO_MOVE_THRESHOLD_PCT": str(
                    watchlist.get("inf_diario_move_threshold_pct", 10.0)
                ),
                "ONCA_INF_DIARIO_TOP_N": str(
                    watchlist.get("inf_diario_top_n") or ""
                ),
                "ONCA_RAW_BUCKET": raw_bucket.bucket_name,
                "ONCA_KB_ID": knowledge_base.attr_knowledge_base_id,
                "ONCA_KB_DATA_SOURCE_ID": data_source.attr_data_source_id,
                # Receita Federal CNPJ (QSA) enrichment of new entrants.
                "ONCA_RECEITA_ENRICH": "true",
                "ONCA_RECEITA_MAX": "15",
            },
        )
        state_table.grant_read_write_data(func)
        entities_table.grant_read_write_data(func)
        digests_bucket.grant_put(func)
        raw_bucket.grant_put(func)
        func.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:StartIngestionJob"],
                resources=[knowledge_base.attr_knowledge_base_arn],
            )
        )

        # Phase 2 Stage B: synthesis / correlation Lambda (digest-first;
        # optional KB Retrieve + Converse when quotas allow).
        synth = lambda_.Function(
            self,
            "OncaSynthesisLambda",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.lambda_handler.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_RAW_BUCKET": raw_bucket.bucket_name,
                "ONCA_KB_ID": knowledge_base.attr_knowledge_base_id,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_SYNTH_MAX_CANDIDATES": "10",
                # Live synthesis on: Titan V2 embed quota (60 RPM, approved
                # 2026-08-10) unblocked KB ingestion; nova-lite Converse + KB
                # Retrieve verified against this account 2026-08-13.
                "ONCA_SYNTH_USE_LLM": "true",
                "ONCA_SYNTH_USE_KB": "true",
                # Emit-on-change: only surface narratives when an entity's
                # signals actually changed (new doc / threshold move), so the
                # feed reports change, not daily steady-state restatements.
                "ONCA_SYNTH_CHANGE_ONLY": "true",
                # Quality bar: multi-lens preferred; solo only for new high-value.
                "ONCA_SYNTH_MIN_LENSES": "2",
                # Aligned with the dashboard's MÉDIO tier floor (real scoring v1).
                "ONCA_SYNTH_MIN_SCORE": "0.40",
                "ONCA_SYNTH_MIN_FUND_PL": "100000000",
                "ONCA_ROUTER_MODEL_ID": "amazon.nova-micro-v1:0",
                "ONCA_SYNTH_MODEL_ID": "amazon.nova-lite-v1:0",
            },
        )
        digests_bucket.grant_read(synth)
        digests_bucket.grant_put(synth)
        raw_bucket.grant_read(synth)
        entities_table.grant_read_write_data(synth)
        synth.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:Retrieve",
                    "bedrock:RetrieveAndGenerate",
                ],
                resources=[knowledge_base.attr_knowledge_base_arn],
            )
        )
        synth.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:Converse",
                ],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                ],
            )
        )

        # Phase 3 warroom dashboard: static site (S3 + CloudFront), fed by a
        # pre-aggregated feed.json. No API/backend — data changes once daily, so
        # a static file keeps cost at the CloudFront request floor (no idle cost).
        site_bucket = s3.Bucket(
            self,
            "OncaDashboardSite",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )

        # Basic auth at the CloudFront edge (viewer-request). Prototype gate for a
        # competitive-intel feed. Credentials resolve from SSM (durable across
        # deploys) with an ONCA_DASH_* env override — see dashboard_credentials().
        dash_user, dash_pass = dashboard_credentials()
        basic = base64.b64encode(f"{dash_user}:{dash_pass}".encode()).decode()
        auth_fn = cloudfront.Function(
            self,
            "OncaDashboardBasicAuth",
            code=cloudfront.FunctionCode.from_inline(
                "function handler(event) {\n"
                "  var r = event.request; var h = r.headers;\n"
                f'  var expected = "Basic {basic}";\n'
                "  if (!h.authorization || h.authorization.value !== expected) {\n"
                "    return { statusCode: 401, statusDescription: 'Unauthorized',\n"
                "      headers: { 'www-authenticate': { value: 'Basic realm=\"Onca Warroom\"' } } };\n"
                "  }\n"
                "  return r;\n"
                "}\n"
            ),
        )

        distribution = cloudfront.Distribution(
            self,
            "OncaDashboardCdn",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=cf_origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=auth_fn,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            comment="Onca warroom dashboard",
            # Pilot traffic monitor: standard access logs to an auto-provisioned
            # S3 bucket (per-request detail — IP, URI, status, referer).
            enable_logging=True,
            log_file_prefix="cf-access/",
        )

        # And a CloudWatch dashboard to watch pilot traffic at a glance.
        def _cf_metric(name: str, statistic: str) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace="AWS/CloudFront",
                metric_name=name,
                dimensions_map={
                    "DistributionId": distribution.distribution_id,
                    "Region": "Global",
                },
                statistic=statistic,
                period=Duration.hours(1),
            )

        traffic = cloudwatch.Dashboard(
            self,
            "OncaDashboardTraffic",
            dashboard_name="Onca-Warroom-Traffic",
        )
        traffic.add_widgets(
            cloudwatch.GraphWidget(
                title="Requests (hourly)",
                left=[_cf_metric("Requests", "Sum")],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="Error rate % (4xx / 5xx)",
                left=[
                    _cf_metric("4xxErrorRate", "Average"),
                    _cf_metric("5xxErrorRate", "Average"),
                ],
                width=12,
            ),
        )

        # Deploy the static frontend (index.html). prune=False so the Lambda's
        # feed.json isn't deleted on redeploys; invalidate index.html each time.
        s3deploy.BucketDeployment(
            self,
            "OncaDashboardDeploy",
            sources=[s3deploy.Source.asset(str(SITE_ASSET))],
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/index.html"],
            prune=False,
        )

        # Feed builder: aggregate recent narratives -> feed.json in the site bucket.
        feed_fn = lambda_.Function(
            self,
            "OncaFeedBuilder",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.dashboard.feed_builder.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_SITE_BUCKET": site_bucket.bucket_name,
                "ONCA_FEED_WINDOW_DAYS": "14",
            },
        )
        digests_bucket.grant_read(feed_fn)
        site_bucket.grant_put(feed_fn)

        # Orchestration: one daily pipeline ordering ingest -> synth -> feed,
        # replacing the two independent schedules. Sequential execution guarantees
        # synth reads the digest this run's ingest just wrote (digest_io picks the
        # newest object in lambda-digests/) and the feed builder sees synth's
        # fresh narratives. Each task gets an empty payload so synth never mistakes
        # the ingest Lambda's {statusCode, body} return for a digest. No
        # KB-ingestion wait: synth is digest-first and KB Retrieve only enriches
        # the already-embedded corpus, so today's newest docs lagging one run is
        # acceptable.
        ingest_task = sfn_tasks.LambdaInvoke(
            self,
            "IngestTask",
            lambda_function=func,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.ingest",
        )
        ingest_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(30),
            backoff_rate=2.0,
        )
        synth_task = sfn_tasks.LambdaInvoke(
            self,
            "SynthTask",
            lambda_function=synth,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.synth",
        )
        synth_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(30),
            backoff_rate=2.0,
        )
        feed_task = sfn_tasks.LambdaInvoke(
            self,
            "FeedTask",
            lambda_function=feed_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.feed",
        )
        feed_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(30),
            backoff_rate=2.0,
        )

        pipeline = sfn.StateMachine(
            self,
            "OncaPipeline",
            definition_body=sfn.DefinitionBody.from_chainable(
                ingest_task.next(synth_task).next(feed_task)
            ),
            # Budget for a 15-min ingest (plus a retry), then synth, then feed.
            timeout=Duration.minutes(45),
        )

        # Intraday triggers. Brazil is UTC-3 year-round (no DST since 2019), so
        # these UTC crons map to fixed BRT wall-clock times and never drift.
        # Times land fresh feeds inside corporate office hours (08–18 BRT), just
        # after the main gov publish windows. Add more (label, utc_h, utc_m)
        # tuples to grow 3 -> 5 runs/day; each gets its own rule + target.
        pipeline_runs = [
            ("Abertura", 9, 45),     # 06:45 BRT — DOU AM edition + overnight CVM/SEC
            ("MeioDia", 15, 30),     # 12:30 BRT — morning BCB acts + AM news
            ("Fechamento", 20, 30),  # 17:30 BRT — afternoon filings + SEC US-morning
        ]
        for label, utc_hour, utc_minute in pipeline_runs:
            rule = events.Rule(
                self,
                f"OncaPipelineSchedule{label}",
                schedule=events.Schedule.cron(
                    minute=str(utc_minute), hour=str(utc_hour)
                ),
                enabled=True,
            )
            rule.add_target(targets.SfnStateMachine(pipeline))

        CfnOutput(
            self,
            "DashboardUrl",
            value=f"https://{distribution.distribution_domain_name}",
            description="Onca warroom dashboard (basic auth)",
        )
        CfnOutput(
            self,
            "EntitiesTableName",
            value=entities_table.table_name,
            description="Entities registry table (seed with src.synth.entity_registry)",
        )
        CfnOutput(
            self,
            "TrafficDashboardUrl",
            value=(
                f"https://console.aws.amazon.com/cloudwatch/home?region={self.region}"
                f"#dashboards:name={traffic.dashboard_name}"
            ),
            description="CloudWatch traffic monitor for the pilot dashboard",
        )


app = App()
OncaPrototypeStack(app, "OncaPrototypeStack")
app.synth()
