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

        # Feature store (ADR 003 Wave 0): derive per-entity rolling features from
        # the durable narrative history (never raw) -> features/latest.json. Runs
        # BEFORE synth in the pipeline so Wave 1 detectors can read it. No LLM.
        feature_fn = lambda_.Function(
            self,
            "OncaFeatureStore",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.feature_store.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_FEATURE_WINDOW_DAYS": "90",
            },
        )
        digests_bucket.grant_read(feature_fn)
        digests_bucket.grant_put(feature_fn)
        entities_table.grant_read_data(feature_fn)

        # Silence detector (ADR 003 Wave 1): reads features/latest.json + recent
        # activity, writes derived "went quiet" narratives back into narratives/.
        # Only touches the digests bucket (no registry, no LLM).
        silence_fn = lambda_.Function(
            self,
            "OncaSilence",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.silence.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_FEATURE_WINDOW_DAYS": "90",
            },
        )
        # read features + history, put silence cards, and DELETE superseded
        # same-day silence cards (retraction) — grant_read_write covers all three.
        digests_bucket.grant_read_write(silence_fn)

        # Longitudinal detector (ADR 003 Wave 1): recomputes fresh features from
        # history and writes derived "broke its own pattern" narratives. Needs the
        # entities table for the industry map on the recompute; digests read/write
        # (put break cards + retract normalized same-day ones).
        longitudinal_fn = lambda_.Function(
            self,
            "OncaLongitudinal",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.longitudinal.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_FEATURE_WINDOW_DAYS": "90",
            },
        )
        digests_bucket.grant_read_write(longitudinal_fn)
        entities_table.grant_read_data(longitudinal_fn)

        # Comparative detector (ADR 003 Wave 1 / ADR 004 SWOT feeder): recomputes
        # fresh features and writes derived "vs. its industry peers" narratives, each
        # carrying a swot_hint for the belief store. Same access as longitudinal.
        comparative_fn = lambda_.Function(
            self,
            "OncaComparative",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.comparative.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_FEATURE_WINDOW_DAYS": "90",
            },
        )
        digests_bucket.grant_read_write(comparative_fn)
        entities_table.grant_read_data(comparative_fn)

        # Thematic detector (ADR 003 Wave 1 / ADR 004 O-T feeder): tags recent activity
        # with a keyword theme taxonomy and writes cross-entity "sector current"
        # narratives (subject = theme). Deterministic, LLM-free; digests read/write
        # (put current cards + retract themes that fell below threshold same-day).
        thematic_fn = lambda_.Function(
            self,
            "OncaThematic",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.thematic.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_FEATURE_WINDOW_DAYS": "90",
            },
        )
        digests_bucket.grant_read_write(thematic_fn)

        # Regulatory-lifecycle detector (ADR 003 Wave 1 / ADR 004 T feeder): threads
        # normative instruments (IN BCB, Resolução, Regulamento do Pix) out of the
        # regulatory narratives, extracts best-effort deadlines, infers the affected
        # domain, and writes "radar regulatório" cards (subject = instrument).
        # Deterministic/LLM-free; digests read/write (put + same-day retract).
        regulatory_fn = lambda_.Function(
            self,
            "OncaRegulatory",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.regulatory.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_FEATURE_WINDOW_DAYS": "90",
            },
        )
        digests_bucket.grant_read_write(regulatory_fn)

        # Cohort/vintage detector (ADR 003 Wave 1 / ADR 004 O-T feeder): set-longitudinal
        # over industry cohorts — recomputes each segment's recent-vs-baseline threat
        # "temperature" from the narrative history + registry industry map and writes
        # derived "movimento de cohort" narratives (subject = a set). Needs the entities
        # table for the industry map; digests read/write (put + same-day retract).
        cohort_fn = lambda_.Function(
            self,
            "OncaCohort",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.cohort.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_FEATURE_WINDOW_DAYS": "90",
            },
        )
        digests_bucket.grant_read_write(cohort_fn)
        entities_table.grant_read_data(cohort_fn)

        # SWOT belief store (ADR 004 step 2 v1): rebuilds the per-entity S/W/O/T belief
        # files from the Wave-1 axes' swot_hints (deterministic, no LLM/embeddings yet)
        # and publishes swot/{entity}.json + swot/index.json into the digests bucket.
        swot_fn = lambda_.Function(
            self,
            "OncaSwot",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.swot_store.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_FEATURE_WINDOW_DAYS": "90",
            },
        )
        digests_bucket.grant_read_write(swot_fn)

        # SWOT reconcile-against-belief (ADR 004 step 3): embeds today's free-text
        # narratives (Titan), retrieves the top-k nearest belief bullets, and LLM
        # stance-classifies each pair. Reinforcements auto-apply (durable evidence
        # the swot store folds in next rebuild); contradictions & new bullets are
        # PROPOSED only (swot/proposals.json) — never auto-applied. Needs Bedrock
        # (Converse for stance + InvokeModel for Titan embeddings).
        reconcile_fn = lambda_.Function(
            self,
            "OncaSwotReconcile",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.swot_reconcile.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_RECONCILE_ENABLED": "1",
                "ONCA_RECONCILE_WINDOW_DAYS": "3",
                "ONCA_SYNTH_MODEL_ID": "amazon.nova-lite-v1:0",
                "ONCA_EMBED_MODEL_ID": "amazon.titan-embed-text-v2:0",
            },
        )
        digests_bucket.grant_read_write(reconcile_fn)
        reconcile_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:Converse"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                ],
            )
        )

        # Incident thread store (ADR 003 Wave 2): threads related developments into
        # living incident docs (event identity by entity+event-type) with an
        # open/developing/resolved lifecycle; publishes threads/{id}.json + a
        # threads/index.json of feed-ready cards. Deterministic, LLM-free.
        threads_fn = lambda_.Function(
            self,
            "OncaThreads",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.threads.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_FEATURE_WINDOW_DAYS": "90",
            },
        )
        digests_bucket.grant_read_write(threads_fn)

        # Behavioral detector (ADR 003 Wave 2): matches an entity's own activity against
        # pattern templates (drumbeat = regular cadence; multi-front = breadth of event
        # types) and writes derived behavioral-signature cards. Needs the entities table
        # for the industry map on the feature recompute.
        behavioral_fn = lambda_.Function(
            self,
            "OncaBehavioral",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.behavioral.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_FEATURE_WINDOW_DAYS": "90",
            },
        )
        digests_bucket.grant_read_write(behavioral_fn)
        entities_table.grant_read_data(behavioral_fn)

        # Relational graph (ADR 003 Wave 3): builds typed entity-pair edges from the
        # durable narratives (co_mention/convergence/dispute); factual co_mention edges
        # become cards, interpretive edges (convergence/dispute) are PROPOSED only into
        # a review queue (never auto-published — defamation guardrail). Deterministic.
        relational_fn = lambda_.Function(
            self,
            "OncaRelational",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.relational.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_FEATURE_WINDOW_DAYS": "90",
            },
        )
        digests_bucket.grant_read_write(relational_fn)

        # Operatives / person layer (ADR 003 Wave 3, Shift 3): promotes public-record
        # person names (QSA sócios, DOU parties) into review-gated person nodes + role
        # edges, LGPD-scoped (no CPF, public professional roles only). Source-gated
        # today (ingestion carries no person names yet); activates when they land.
        operatives_fn = lambda_.Function(
            self,
            "OncaOperatives",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.operatives.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
            },
        )
        digests_bucket.grant_read_write(operatives_fn)

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
                # Read the entities review queue (ADR step 5) to surface pending
                # group-merge proposals in the dashboard (read-only).
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
            },
        )
        digests_bucket.grant_read(feed_fn)
        site_bucket.grant_put(feed_fn)
        entities_table.grant_read_data(feed_fn)

        # Review-queue write endpoint (ADR step 5). Fronted by the SAME basic-auth
        # CloudFront Function as the dashboard (see the /api/* behavior below), so
        # the browser's existing credentials authorize approve/reject. The Function
        # URL is AuthType NONE; a shared origin secret (CloudFront injects it as a
        # custom header) blocks calling the URL directly. Override via env for a
        # real secret; the default is defense-in-depth behind basic-auth.
        origin_secret = os.environ.get("ONCA_ORIGIN_SECRET", "onca-review-origin-v1")
        review_fn = lambda_.Function(
            self,
            "OncaReviewAction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.dashboard.review_action.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_FEED_BUILDER_NAME": feed_fn.function_name,
                "ONCA_ORIGIN_SECRET": origin_secret,
            },
        )
        entities_table.grant_read_write_data(review_fn)
        feed_fn.grant_invoke(review_fn)
        review_url = review_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE
        )

        # Registry CRUD API (operator control plane): full curation over the ENT#
        # records. Same auth model as the review endpoint (basic-auth edge +
        # origin secret). CloudFront matches cache behaviors by INSERTION ORDER,
        # not specificity — so the more-specific /api/registry/* MUST be added
        # BEFORE the /api/* catch-all below, or registry calls fall through to the
        # review action. (This ordering is load-bearing; do not reorder.)
        registry_fn = lambda_.Function(
            self,
            "OncaRegistryApi",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.dashboard.registry_api.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_ORIGIN_SECRET": origin_secret,
            },
        )
        entities_table.grant_read_write_data(registry_fn)
        registry_url = registry_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE
        )
        distribution.add_behavior(
            "/api/registry/*",
            cf_origins.FunctionUrlOrigin(
                registry_url, custom_headers={"X-Onca-Origin": origin_secret}
            ),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            function_associations=[
                cloudfront.FunctionAssociation(
                    function=auth_fn,
                    event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                )
            ],
        )
        # Ad-hoc "run soon" trigger: schedules ONE pipeline run at the top of the
        # next hour, debounced (fixed-name EventBridge Scheduler one-shot). Same
        # auth model; registered BEFORE the /api/* catch-all (insertion order).
        # Pipeline-dependent env (ONCA_PIPELINE_ARN / ONCA_SCHEDULER_ROLE_ARN) is
        # attached after the state machine is defined, below.
        run_fn = lambda_.Function(
            self,
            "OncaRunTrigger",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.dashboard.run_trigger.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_ORIGIN_SECRET": origin_secret,
                "ONCA_SCHEDULE_NAME": "onca-adhoc-run",
            },
        )
        run_url = run_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE
        )
        distribution.add_behavior(
            "/api/run/*",
            cf_origins.FunctionUrlOrigin(
                run_url, custom_headers={"X-Onca-Origin": origin_secret}
            ),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            function_associations=[
                cloudfront.FunctionAssociation(
                    function=auth_fn,
                    event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                )
            ],
        )
        # /api/* catch-all (review action) — registered AFTER /api/registry/* and
        # /api/run/* so the specific patterns win; everything else under /api/ lands here.
        distribution.add_behavior(
            "/api/*",
            cf_origins.FunctionUrlOrigin(
                review_url, custom_headers={"X-Onca-Origin": origin_secret}
            ),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            function_associations=[
                cloudfront.FunctionAssociation(
                    function=auth_fn,
                    event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                )
            ],
        )

        # Orchestration: one daily pipeline ordering ingest -> synth -> feed,
        # replacing the two independent schedules. Sequential execution guarantees
        # synth reads the digest this run's ingest just wrote (digest_io picks the
        # newest object in lambda-digests/) and the feed builder sees synth's
        # fresh narratives. Each task gets an empty payload so synth never mistakes
        # the ingest Lambda's {statusCode, body} return for a digest. No
        # KB-ingestion wait: synth is digest-first and KB Retrieve only enriches
        # the already-embedded corpus, so today's newest docs lagging one run is
        # acceptable.
        # Ingest is split into two PARALLEL branches on the same Lambda (mode via
        # payload): the fixed-cost STRUCTURED sources and the registry-linear NEWS
        # fetch. News no longer shares a wall-clock budget with structured, so the
        # news path scales with the registry without starving structured. Each
        # branch writes a disjoint S3 object; synth overlays them (digest_io).
        structured_ingest = sfn_tasks.LambdaInvoke(
            self,
            "StructuredIngest",
            lambda_function=func,
            payload=sfn.TaskInput.from_object({"mode": "structured"}),
            result_path=sfn.JsonPath.DISCARD,
        )
        structured_ingest.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(30),
            backoff_rate=2.0,
        )
        news_ingest = sfn_tasks.LambdaInvoke(
            self,
            "NewsIngest",
            lambda_function=func,
            payload=sfn.TaskInput.from_object({"mode": "news"}),
            result_path=sfn.JsonPath.DISCARD,
        )
        news_ingest.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(30),
            backoff_rate=2.0,
        )
        ingest_task = sfn.Parallel(self, "Ingest", result_path=sfn.JsonPath.DISCARD)
        ingest_task.branch(structured_ingest)
        ingest_task.branch(news_ingest)
        feature_task = sfn_tasks.LambdaInvoke(
            self,
            "FeatureTask",
            lambda_function=feature_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.features",
        )
        feature_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
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
        silence_task = sfn_tasks.LambdaInvoke(
            self,
            "SilenceTask",
            lambda_function=silence_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.silence",
        )
        silence_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        longitudinal_task = sfn_tasks.LambdaInvoke(
            self,
            "LongitudinalTask",
            lambda_function=longitudinal_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.longitudinal",
        )
        longitudinal_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        comparative_task = sfn_tasks.LambdaInvoke(
            self,
            "ComparativeTask",
            lambda_function=comparative_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.comparative",
        )
        comparative_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        thematic_task = sfn_tasks.LambdaInvoke(
            self,
            "ThematicTask",
            lambda_function=thematic_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.thematic",
        )
        thematic_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        regulatory_task = sfn_tasks.LambdaInvoke(
            self,
            "RegulatoryTask",
            lambda_function=regulatory_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.regulatory",
        )
        regulatory_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        cohort_task = sfn_tasks.LambdaInvoke(
            self,
            "CohortTask",
            lambda_function=cohort_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.cohort",
        )
        cohort_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        swot_task = sfn_tasks.LambdaInvoke(
            self,
            "SwotTask",
            lambda_function=swot_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.swot",
        )
        swot_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        reconcile_task = sfn_tasks.LambdaInvoke(
            self,
            "SwotReconcileTask",
            lambda_function=reconcile_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.reconcile",
        )
        reconcile_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        threads_task = sfn_tasks.LambdaInvoke(
            self,
            "ThreadsTask",
            lambda_function=threads_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.threads",
        )
        threads_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        behavioral_task = sfn_tasks.LambdaInvoke(
            self,
            "BehavioralTask",
            lambda_function=behavioral_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.behavioral",
        )
        behavioral_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        relational_task = sfn_tasks.LambdaInvoke(
            self,
            "RelationalTask",
            lambda_function=relational_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.relational",
        )
        relational_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        operatives_task = sfn_tasks.LambdaInvoke(
            self,
            "OperativesTask",
            lambda_function=operatives_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.operatives",
        )
        operatives_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
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
                ingest_task.next(feature_task)
                .next(synth_task)
                .next(silence_task)
                .next(longitudinal_task)
                .next(comparative_task)
                .next(thematic_task)
                .next(regulatory_task)
                .next(cohort_task)
                .next(swot_task)
                .next(reconcile_task)
                .next(threads_task)
                .next(behavioral_task)
                .next(relational_task)
                .next(operatives_task)
                .next(feed_task)
            ),
            # Budget for a 15-min ingest (plus a retry), then synth, then feed.
            timeout=Duration.minutes(45),
        )

        # Ad-hoc run trigger (OncaRunTrigger, defined above): the Lambda creates a
        # one-shot EventBridge Scheduler that StartExecutions this pipeline. The
        # Scheduler assumes this role; the Lambda may create/update/read that one
        # named schedule and pass this role to the scheduler service.
        scheduler_role = iam.Role(
            self,
            "OncaAdhocSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        pipeline.grant_start_execution(scheduler_role)
        # Live status read for the dashboard's "Executar" button: ListExecutions /
        # DescribeExecution / GetExecutionHistory so the latest run's current step
        # can be surfaced (GET /api/run/). Read-only — write stays with the scheduler.
        pipeline.grant_read(run_fn)
        run_fn.add_environment("ONCA_PIPELINE_ARN", pipeline.state_machine_arn)
        run_fn.add_environment("ONCA_SCHEDULER_ROLE_ARN", scheduler_role.role_arn)
        adhoc_schedule_arn = Stack.of(self).format_arn(
            service="scheduler",
            resource="schedule",
            resource_name="default/onca-adhoc-run",
        )
        run_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "scheduler:CreateSchedule",
                    "scheduler:UpdateSchedule",
                    "scheduler:GetSchedule",
                    "scheduler:DeleteSchedule",
                ],
                resources=[adhoc_schedule_arn],
            )
        )
        run_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[scheduler_role.role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
            )
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
