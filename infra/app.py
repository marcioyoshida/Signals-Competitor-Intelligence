"""CDK app for the Phase 1.5 Lambda prototype.

This deploys a scheduled Lambda that runs the ingestion prototype and can
later be extended to S3/DynamoDB persistence.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

import yaml
from aws_cdk import App, CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_auth
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_int
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_cloudfront_origins as cf_origins
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import custom_resources as cr
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
        # Cost Explorer groups Bedrock + other spend by this allocation tag
        # (scripts/daily_cost_tracker.py --tag tr:project-name). Override the
        # value per deploy with ONCA_PROJECT_NAME; activate the key in CE after
        # the first tagged resource appears (~24h).
        Tags.of(self).add(
            "tr:project-name", os.environ.get("ONCA_PROJECT_NAME", "onca")
        )

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

        # Phase D — per-tenant entitlement source of truth (ADR 002 Phase D + ADR 016).
        # tenant_id -> {tier ∈ entry|saas|sovereign, modules[]}. The read boundary scopes
        # the feed/agent to modules[]; tier selects the delivery plane. No record = no
        # entitlement (fail closed). PK reserved in ADR 001.
        tenant_config_table = dynamodb.Table(
            self,
            "OncaTenantConfig",
            partition_key=dynamodb.Attribute(
                name="tenant_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        CfnOutput(self, "TenantConfigTable", value=tenant_config_table.table_name)

        # ADR 018 Phase 1b — append-only curation mutation journal (audit + rollback).
        # Separate table so it never bloats the entity scans; the registry writers below
        # get ONCA_CURATION_LOG_TABLE + write access.
        curation_log_table = dynamodb.Table(
            self,
            "OncaCurationLog",
            partition_key=dynamodb.Attribute(name="entity_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="ts", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        CfnOutput(self, "CurationLogTable", value=curation_log_table.table_name)

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
                # Entity discovery (ADR 011): CVM FIAGRO structured sync → registry.
                # Flipped LIVE 2026-08-29 after a read-only dry-run + name-quality
                # gate (auto-create only ticker/clean-brand funds; junk → review).
                "ONCA_ENTITY_DISCOVERY": "true",
                "ONCA_ENTITY_DISCOVERY_AUTOCREATE": "true",
                "ONCA_FIAGRO_MIN_PL": "50000000",
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
                # One Google-News HTTP per term: cap must cover the whole registry
                # (121 terms and growing) or the newest entities past the cap are
                # silently never queried. Budget raised to fit ~1.1s/term within
                # the 900s Lambda timeout (news runs as its own branch).
                "ONCA_NEWS_MAX_TERMS": str(watchlist.get("news_max_terms", 200)),
                "ONCA_SOURCE_TIMEOUT_SEC": str(watchlist.get("source_timeout_sec", 300)),
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
        curation_log_table.grant_write_data(func)  # ADR 018 Phase 1b
        func.add_environment("ONCA_CURATION_LOG_TABLE", curation_log_table.table_name)
        digests_bucket.grant_put(func)
        # GOV_DADOS_TOKEN access (#63 / Tier-3): the ingester reads the token from the
        # api-key secret (via boto3) to reach the dados.gov.br catalog. The token now
        # authenticates and catalog search/resource-resolution work live. Grant read on
        # that one secret so any dados.gov.br source can use it.
        func.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}"
                    ":secret:signalscompetitor/onca/api-key-*"
                ],
            )
        )
        # consumidor.gov.br (#63) stays default-off: the catalog resolves its resources to
        # dados.mj.gov.br, which is currently NXDOMAIN (the MJ open-data host is dead), so
        # the file is unreachable through the catalog too. Flip ONCA_CONSUMIDOR_GOV=true
        # once a live resource host is confirmed.
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
            # 1536MB ≈ 1 vCPU (Lambda CPU scales with memory). candidate
            # extraction is CPU-bound on resolve_entities, whose cost grows with
            # the registry (discovery keeps adding entities); 512MB (~0.36 vCPU)
            # risked the 5-min timeout. Pairs with resolve_entities memoization.
            memory_size=1536,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_RAW_BUCKET": raw_bucket.bucket_name,
                "ONCA_KB_ID": knowledge_base.attr_knowledge_base_id,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                # Deferred news-commit (issue #23): synth marks the fetched news
                # ids seen only after it has consumed the slice, so it needs the
                # trade_press seen-set state table.
                "ONCA_STATE_TABLE": state_table.table_name,
                # Narratives synthesised per run. Raised 10->30 as the registry
                # grew past 120 entities (#14/#16/#18/#25/#26/#27): a cap of 10
                # starved lower-ranked entities of a narrative slot, so their
                # (now un-burned, #23) news never surfaced and they read silent.
                # Comfortably within the 5-min synth budget at nova-lite cost.
                "ONCA_SYNTH_MAX_CANDIDATES": "30",
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
        curation_log_table.grant_write_data(synth)  # ADR 018 Phase 1b
        synth.add_environment("ONCA_CURATION_LOG_TABLE", curation_log_table.table_name)
        state_table.grant_read_write_data(synth)  # deferred news seen-commit (#23)
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
                "  // Clean-route rewrite (v2 multi-context dashboards), evaluated\n"
                "  // BEFORE the basic-auth gate: /admin, /newentry, /adquirencia,\n"
                "  // /fintech, /seguros, /wealth (with or without a trailing slash)\n"
                "  // map to their /v2/<ctx>/index.html object. The root '/',\n"
                "  // /entry/*, static assets and every /api/* behavior pass through\n"
                "  // untouched; auth still gates everything below.\n"
                '  var routes = { "/admin": "admin", "/newentry": "newentry",\n'
                '    "/adquirencia": "adquirencia", "/fintech": "fintech",\n'
                '    "/seguros": "seguros", "/wealth": "wealth" };\n'
                "  var key = r.uri;\n"
                '  if (key.length > 1 && key.charAt(key.length - 1) === "/") {\n'
                "    key = key.substring(0, key.length - 1);\n"
                "  }\n"
                '  if (routes[key]) { r.uri = "/v2/" + routes[key] + "/index.html"; }\n'
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

        # --- Phase C: identity (Cognito) — ADR 002 Decision 7 "7 gates 6" --------
        # Per-tenant identity is THE prerequisite for entitlement + the distribution
        # tiers (ADR 015/016): the shared basic-auth edge cannot attribute a request to
        # a tenant. This user pool is the identity provider; `tenant` + `tier` ride as
        # custom claims that onca-tenant-config (Phase D) authorizes against.
        # ADDITIVE — it does NOT yet gate the Lambda-URL APIs. Those are Lambda URLs
        # (no built-in authorizer), so JWT verification is a follow-on increment
        # (API Gateway JWT authorizer vs Lambda@Edge vs in-Lambda JWKS — see ADR).
        _acct = os.environ.get("CDK_DEFAULT_ACCOUNT", "signals")
        user_pool = cognito.UserPool(
            self,
            "OncaUserPool",
            self_sign_up_enabled=False,  # tenants are provisioned, not open self-signup
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False),
            ),
            custom_attributes={
                # the tenant this user belongs to (entitlement key) + its tier.
                "tenant": cognito.StringAttribute(mutable=False),
                "tier": cognito.StringAttribute(mutable=True),  # entry | saas | sovereign
            },
            password_policy=cognito.PasswordPolicy(min_length=12),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.RETAIN,  # never destroy identities on stack update
        )
        user_pool_client = user_pool.add_client(
            "OncaWarroomClient",
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"https://{distribution.distribution_domain_name}/"],
                logout_urls=[f"https://{distribution.distribution_domain_name}/"],
            ),
            prevent_user_existence_errors=True,
        )
        user_pool_domain = user_pool.add_domain(
            "OncaHostedUi",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"onca-{_acct}"),
        )
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(
            self,
            "UserPoolHostedUi",
            value=(
                f"https://{user_pool_domain.domain_name}.auth."
                f"{self.region}.amazoncognito.com"
            ),
        )

        # --- Demo SaaS tenant provisioning (deploy-time, idempotent) --------------
        # ADR 016 SaaS-plane demo: four single-module `saas` tenants + one Cognito
        # user each (tenant/tier ride as custom claims the JWT authorizer forwards to
        # /api/feed). Provisioned ON DEPLOY so a fresh account has a working per-tenant
        # feed without a manual seed. tier=`saas` ⇒ unrestricted module allow-list
        # (see allowed_industries_for_tier), so writing these rows directly can NOT
        # weaken the entry-tier cap — a real `entry` tenant is still validated/capped
        # by put_tenant_config on provision. Kept in sync with the v2 dashboard
        # contexts: acquiring↔adquirencia, fintech↔fintech, insurance↔seguros,
        # wealth-management↔wealth (the newentry portal is entry-tier, not here).
        demo_tenants = [
            ("acquiring", "acquiring"),
            ("fintech", "fintech"),
            ("insurance", "insurance"),
            ("wealth-management", "wealth-management"),
        ]
        # onca-tenant-config rows via a single idempotent BatchWriteItem custom
        # resource (re-runs each deploy; PutRequest overwrites ⇒ self-healing). Mirrors
        # the {tenant_id, tier, modules, plane} shape put_tenant_config writes.
        _put_requests = [
            {
                "PutRequest": {
                    "Item": {
                        "tenant_id": {"S": tid},
                        "tier": {"S": "saas"},
                        "modules": {"L": [{"S": slug}]},
                        "plane": {"S": "saas"},
                    }
                }
            }
            for (tid, slug) in demo_tenants
        ]
        _seed_call = cr.AwsSdkCall(
            service="DynamoDB",
            action="batchWriteItem",
            parameters={"RequestItems": {tenant_config_table.table_name: _put_requests}},
            physical_resource_id=cr.PhysicalResourceId.of("onca-demo-tenant-seed"),
        )
        cr.AwsCustomResource(
            self,
            "OncaDemoTenantSeed",
            on_create=_seed_call,
            on_update=_seed_call,
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=[tenant_config_table.table_arn]
            ),
            install_latest_aws_sdk=False,
        )
        # One demo Cognito user per tenant. SUPPRESS ⇒ no invite email to the
        # placeholder .example address; email_verified skips verification. Users land
        # in FORCE_CHANGE_PASSWORD — an operator sets a permanent demo password
        # out-of-band (the one unavoidable manual step; see deploy notes).
        for (tid, _slug) in demo_tenants:
            _uid = tid.replace("-", "").capitalize()
            cognito.CfnUserPoolUser(
                self,
                f"OncaDemoUser{_uid}",
                user_pool_id=user_pool.user_pool_id,
                username=f"demo-{tid}@onca.example",
                message_action="SUPPRESS",
                user_attributes=[
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email", value=f"demo-{tid}@onca.example"),
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email_verified", value="true"),
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="custom:tenant", value=tid),
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="custom:tier", value="saas"),
                ],
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
            # /entry/index.html = the ADR 016 Entry Portal (thin shared static site
            # that serves ONLY feed.entry.json — no higher-tier industry can leak).
            distribution_paths=[
                "/index.html",
                "/entry/index.html",
                # v2 multi-context dashboards (six clean routes + shared assets).
                "/v2/admin/index.html",
                "/v2/newentry/index.html",
                "/v2/adquirencia/index.html",
                "/v2/fintech/index.html",
                "/v2/seguros/index.html",
                "/v2/wealth/index.html",
                "/v2/app.css",
                "/v2/app.js",
                "/v2/context.js",
            ],
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

        # SWOT cold-start seeding (ADR 004 step 4): for a thin, well-grounded
        # competitor (few/no belief bullets), LLM-draft an initial SWOT from its own
        # narrative history + registry dossier. PROPOSED only (swot/seed_proposals.json)
        # — never an active bullet; the Phase C vetting UI promotes accepted drafts.
        # Cold-start only, drafted once per entity (idempotent). Needs Bedrock
        # (Converse) + read on the entities table for the dossier.
        seed_fn = lambda_.Function(
            self,
            "OncaSwotSeed",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.swot_seed.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_SEED_ENABLED": "1",
                "ONCA_SYNTH_MODEL_ID": "amazon.nova-lite-v1:0",
            },
        )
        digests_bucket.grant_read_write(seed_fn)
        entities_table.grant_read_data(seed_fn)
        seed_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:Converse"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                ],
            )
        )

        # SWOT belief maintenance (ADR 004 open question #4): a curated (analyst-pinned)
        # bullet unreinforced for ONCA_MAINT_STALE_DAYS earns a `stale` re-review proposal
        # so drift is caught. Deterministic, LLM-free; approve retires the bullet, reject
        # re-affirms it (via the Phase C vetting endpoint). Reads the durable curated +
        # reinforcement stores; writes swot/maintenance_proposals.json.
        maintenance_fn = lambda_.Function(
            self,
            "OncaSwotMaintenance",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.swot_maintenance.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(2),
            memory_size=256,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_MAINT_ENABLED": "1",
                "ONCA_MAINT_STALE_DAYS": "90",
            },
        )
        digests_bucket.grant_read_write(maintenance_fn)

        # TOWS strategic posture pairing (ADR 006): reads active SWOT beliefs and
        # pairs S×O/S×T/W×O/W×T strategic postures via a bounded LLM call. Proposed
        # only — flows through Phase C vetting. Needs Bedrock for the draft.
        tows_fn = lambda_.Function(
            self,
            "OncaTows",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.tows.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(3),
            memory_size=256,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_TOWS_ENABLED": "1",
                "ONCA_TOWS_MAX_ENTITIES": "8",
            },
        )
        digests_bucket.grant_read_write(tows_fn)
        tows_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )

        # Porter's Five Forces (ADR 006 Phase 2): analyzes competitive-structure forces
        # for each entity (rivalry, new_entrants, substitutes, buyer_power, supplier_power)
        # via a bounded LLM call over SWOT beliefs + narrative evidence + industry context.
        # Proposed only — flows through Phase C vetting. Needs Bedrock for the draft +
        # entities table for industry membership.
        porter_fn = lambda_.Function(
            self,
            "OncaPorter",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.porter.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(3),
            memory_size=256,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_PORTER_ENABLED": "1",
                "ONCA_PORTER_MAX_ENTITIES": "8",
            },
        )
        digests_bucket.grant_read_write(porter_fn)
        entities_table.grant_read_data(porter_fn)
        porter_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )

        # PESTLE macro-environmental analysis (ADR 006): analyzes political, economic,
        # social, technological, legal, environmental factors per entity via a bounded
        # LLM call over SWOT beliefs + narrative evidence + industry context.
        pestle_fn = lambda_.Function(
            self,
            "OncaPestle",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.pestle.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(3),
            memory_size=256,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_PESTLE_ENABLED": "1",
                "ONCA_PESTLE_MAX_ENTITIES": "8",
            },
        )
        digests_bucket.grant_read_write(pestle_fn)
        entities_table.grant_read_data(pestle_fn)
        pestle_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )

        # Ansoff Matrix growth-direction classification (ADR 006): classifies each
        # entity's recent strategic moves into penetration/market_dev/product_dev/
        # diversification via a bounded LLM call.
        ansoff_fn = lambda_.Function(
            self,
            "OncaAnsoff",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.ansoff.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(3),
            memory_size=256,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_ANSOFF_ENABLED": "1",
                "ONCA_ANSOFF_MAX_ENTITIES": "8",
            },
        )
        digests_bucket.grant_read_write(ansoff_fn)
        entities_table.grant_read_data(ansoff_fn)
        ansoff_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )

        # BCG Growth-Share Matrix portfolio-position analysis (ADR 006): classifies
        # each entity into star/cash_cow/question_mark/dog based on relative market
        # share and market growth via a bounded LLM call.
        bcg_fn = lambda_.Function(
            self,
            "OncaBcg",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.bcg.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(3),
            memory_size=256,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_BCG_ENABLED": "1",
                "ONCA_BCG_MAX_ENTITIES": "8",
            },
        )
        digests_bucket.grant_read_write(bcg_fn)
        entities_table.grant_read_data(bcg_fn)
        bcg_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )

        # Porter's Four Corners competitor-prediction analysis (ADR 006): analyzes
        # drivers/assumptions/current_strategy/capabilities + derived response_profile
        # per entity via a bounded LLM call.
        four_corners_fn = lambda_.Function(
            self,
            "OncaFourCorners",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.four_corners.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(3),
            memory_size=256,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_FOUR_CORNERS_ENABLED": "1",
                "ONCA_FOUR_CORNERS_MAX_ENTITIES": "8",
            },
        )
        digests_bucket.grant_read_write(four_corners_fn)
        entities_table.grant_read_data(four_corners_fn)
        four_corners_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )

        # McKinsey 7S visible-only analysis (ADR 006): analyzes structure/systems/
        # strategy per entity via a bounded LLM call. The hidden three S's
        # (shared_values, style, skills) are omitted — unsourceable from public OSINT.
        seven_s_fn = lambda_.Function(
            self,
            "OncaSevenS",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.seven_s.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(3),
            memory_size=256,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_SEVEN_S_ENABLED": "1",
                "ONCA_SEVEN_S_MAX_ENTITIES": "8",
            },
        )
        digests_bucket.grant_read_write(seven_s_fn)
        entities_table.grant_read_data(seven_s_fn)
        seven_s_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )

        # Auto-approval (ADR 006): after every framework has published, approve the
        # PENDING framework proposals whose confidence >= a threshold (input param,
        # default 0.0 == approve all; raise ONCA_AUTOAPPROVE_CONF to re-gate) and
        # promote them into swot/curated.json — the same store the manual vetting UI
        # writes. No LLM/Bedrock; touches S3 only.
        autoapprove_fn = lambda_.Function(
            self,
            "OncaAutoApprove",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.curate.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(2),
            memory_size=256,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_AUTOAPPROVE_ENABLED": "1",
                "ONCA_AUTOAPPROVE_CONF": "0.0",
            },
        )
        digests_bucket.grant_read_write(autoapprove_fn)

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
        # P1 — watchlist QSA enrichment (the person-layer INPUT for operatives): fetches
        # the tracked entities' quadro de sócios (BrasilAPI) and writes
        # graph/watchlist_qsa.json with MASKED docs only (never a full CPF). TTL-gated +
        # bounded per run (QSA is slow-changing). Runs BEFORE operatives so the person
        # axis leaves source_gated. Needs registry read (CNPJ roots) + bucket + egress.
        qsa_fn = lambda_.Function(
            self,
            "OncaWatchlistQsa",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.ingest.watchlist_qsa.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_QSA_ENABLED": "1",
                "ONCA_QSA_TTL_DAYS": "30",
                "ONCA_QSA_MAX_LOOKUPS": "10",
                "ONCA_QSA_MAX_PERSONS": "20",
            },
        )
        digests_bucket.grant_read_write(qsa_fn)
        entities_table.grant_read_data(qsa_fn)

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

        # Predictive / leading-indicator (ADR 003 Opportunistic, TIME-GATED): ships the
        # precursor-rule mechanism but publishes forecasts only once the feature store
        # holds enough history AND the axis is enabled — otherwise reports time_gated.
        predictive_fn = lambda_.Function(
            self,
            "OncaPredictive",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.predictive.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_FEATURE_WINDOW_DAYS": "90",
                "ONCA_PREDICTIVE_ENABLED": "0",          # accrues with history; off until mature
                "ONCA_PREDICTIVE_MIN_HISTORY_DAYS": "120",
            },
        )
        digests_bucket.grant_read_write(predictive_fn)
        entities_table.grant_read_data(predictive_fn)

        # Ecosystem / dependency (ADR 003 Opportunistic, SOURCE-GATED): ships the
        # contagion synthesis + dependency-graph builder; emits exposure cards only when
        # a hub with dependents has an incident (or an external dependency source is
        # wired via ONCA_ECOSYSTEM_SOURCE) — otherwise reports source_gated.
        ecosystem_fn = lambda_.Function(
            self,
            "OncaEcosystem",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.synth.ecosystem.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_FEATURE_WINDOW_DAYS": "90",
            },
        )
        digests_bucket.grant_read_write(ecosystem_fn)

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
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_FEED_BUILDER_NAME": feed_fn.function_name,
                "ONCA_ORIGIN_SECRET": origin_secret,
            },
        )
        entities_table.grant_read_write_data(review_fn)
        curation_log_table.grant_write_data(review_fn)  # ADR 018 Phase 1b
        review_fn.add_environment("ONCA_CURATION_LOG_TABLE", curation_log_table.table_name)
        digests_bucket.grant_read_write(review_fn)  # Phase C: vet SWOT/graph proposal stores
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
        curation_log_table.grant_write_data(registry_fn)  # ADR 018 Phase 1b
        registry_fn.add_environment("ONCA_CURATION_LOG_TABLE", curation_log_table.table_name)
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
        # B3 live-quote proxy (issue #43): GET /api/quotes?industry=<slug> returns the
        # industry's representative listed names from Yahoo Finance (free, no token),
        # fetched server-side (Yahoo has no browser CORS) + cached. Same origin-secret +
        # edge-basic-auth gate as the other dashboard endpoints; no data grants needed.
        quotes_fn = lambda_.Function(
            self,
            "OncaQuotesApi",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.dashboard.quotes_api.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={"PYTHONPATH": "/var/task", "ONCA_ORIGIN_SECRET": origin_secret},
        )
        quotes_url = quotes_fn.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)
        distribution.add_behavior(
            "/api/quotes*",
            cf_origins.FunctionUrlOrigin(
                quotes_url, custom_headers={"X-Onca-Origin": origin_secret}
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
        # Agent Q&A (ADR 010): read-only, grounded, curated NL question answering
        # over the tool's own data (feed.json + KB). Same auth model; registered
        # BEFORE the /api/* catch-all (insertion order). It reads the published
        # feed.json from the site bucket and calls Bedrock Converse + KB Retrieve.
        agent_fn = lambda_.Function(
            self,
            "OncaAgent",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.dashboard.agent_ask.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.seconds(60),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_ORIGIN_SECRET": origin_secret,
                "ONCA_SITE_BUCKET": site_bucket.bucket_name,
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,  # coverage-gap store
                "ONCA_KB_ID": knowledge_base.attr_knowledge_base_id,
                "ONCA_SYNTH_MODEL_ID": os.environ.get("ONCA_SYNTH_MODEL_ID", "amazon.nova-lite-v1:0"),
                "ONCA_TENANT_CONFIG_TABLE": tenant_config_table.table_name,  # Phase D
            },
        )
        site_bucket.grant_read(agent_fn)  # reads feed.json
        digests_bucket.grant_read_write(agent_fn)  # capture coverage gaps (ADR-014)
        tenant_config_table.grant_read_data(agent_fn)  # Phase D: entitlement lookup
        agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:Retrieve"],
                resources=[knowledge_base.attr_knowledge_base_arn],
            )
        )
        agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:Converse"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                ],
            )
        )
        # NOTE: /api/ask is CUT OVER to the Cognito-JWT-verified HTTP API below
        # (same origin, no Lambda function URL, no origin secret, no edge basic-auth).
        # The agent Lambda is invoked only through that authorated ingress now, so it
        # no longer needs a public function URL. Its ONCA_ORIGIN_SECRET env stays as a
        # fail-closed backstop (nothing injects X-Onca-Origin on this path, so the
        # dual-gate requires a verified identity).
        # Coverage-gap API (ADR-014): the "Pontos Cegos" dashboard surface + the
        # Remediar button (single-gap remediation: triage → safe backfill → re-ask
        # the agent → resolve). Needs the union of the agent's + registry's access
        # (digests store, registry backfills, feed read, Bedrock verify). Registered
        # BEFORE the /api/* catch-all (insertion order).
        gaps_fn = lambda_.Function(
            self,
            "OncaGapsApi",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.dashboard.gaps_api.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.seconds(90),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_ORIGIN_SECRET": origin_secret,
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_SITE_BUCKET": site_bucket.bucket_name,
                "ONCA_ENTITIES_TABLE": entities_table.table_name,
                "ONCA_KB_ID": knowledge_base.attr_knowledge_base_id,
                "ONCA_SYNTH_MODEL_ID": os.environ.get("ONCA_SYNTH_MODEL_ID", "amazon.nova-lite-v1:0"),
                # Optional: a fine-grained PAT (issues:write) lets Remediar CLOSE the
                # GitHub issue when a gap resolves. Empty -> the gap resolves in the
                # store and the issue is closed later by the CLI/scheduled pipeline.
                "ONCA_GH_TOKEN": os.environ.get("ONCA_GH_TOKEN", ""),
            },
        )
        digests_bucket.grant_read_write(gaps_fn)
        site_bucket.grant_read(gaps_fn)
        entities_table.grant_read_write_data(gaps_fn)  # safe-autofix backfills
        curation_log_table.grant_write_data(gaps_fn)  # ADR 018 Phase 1b
        gaps_fn.add_environment("ONCA_CURATION_LOG_TABLE", curation_log_table.table_name)
        gaps_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:Retrieve"],
                resources=[knowledge_base.attr_knowledge_base_arn],
            )
        )
        gaps_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:Converse"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                ],
            )
        )
        # NOTE: /api/gaps is CUT OVER to the Cognito-JWT HTTP API below (issue #45),
        # same-origin like /api/ask — no Lambda function URL, no origin secret, no edge
        # basic-auth. Its ONCA_ORIGIN_SECRET env stays only as a fail-closed backstop.

        # Per-tenant scoped feed (issue #48, ADR 016 SaaS): GET /api/feed returns the full
        # feed scoped to the caller's tenant.modules (server-authoritative — the client
        # never sees the full feed). JWT-only; the same projection backs the AWS
        # Marketplace in-account plane (identical read boundary in the tenant's account).
        feed_api_fn = lambda_.Function(
            self,
            "OncaFeedApi",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="src.dashboard.feed_api.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            timeout=Duration.seconds(30),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_SITE_BUCKET": site_bucket.bucket_name,
                "ONCA_TENANT_CONFIG_TABLE": tenant_config_table.table_name,
            },
        )
        site_bucket.grant_read(feed_api_fn)  # reads feed.json
        tenant_config_table.grant_read_data(feed_api_fn)  # tenant → modules

        # --- Phase C increment 2: authenticated API path (API Gateway HTTP API +
        # Cognito JWT authorizer) -----------------------------------------------------
        # This HTTP API VERIFIES a Cognito JWT (API Gateway does the crypto) and passes
        # the tenant claims to the SAME Lambdas. `/api/ask` AND `/api/gaps` (GET list +
        # POST /remediate) are CUT OVER to it (see the same-origin CloudFront behaviors
        # below) — their public Lambda function URLs + origin-secret path are gone. The
        # operator surfaces (registry/run/review) remain origin-secret + edge basic-auth.
        jwt_authorizer = apigwv2_auth.HttpJwtAuthorizer(
            "OncaJwtAuthorizer",
            f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
            identity_source=["$request.header.Authorization"],
            jwt_audience=[user_pool_client.user_pool_client_id],  # Cognito ID-token aud
        )
        auth_api = apigwv2.HttpApi(
            self,
            "OncaAuthApi",
            api_name="onca-auth-api",
            # `/api/ask` is now SAME-ORIGIN via CloudFront, so no CORS preflight is
            # needed for the dashboard. Keep a permissive-origin CORS only so the API
            # stays directly callable (e.g. from tooling) — a bearer token is the gate,
            # never a cookie, so a wildcard origin is safe (no allow_credentials). This
            # also breaks the CloudFront<->HTTP API circular dependency that referencing
            # the distribution's own domain here would create.
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_headers=["authorization", "content-type"],
                allow_methods=[apigwv2.CorsHttpMethod.POST, apigwv2.CorsHttpMethod.GET],
                allow_origins=["*"],
            ),
        )
        auth_api.add_routes(
            path="/api/ask",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_int.HttpLambdaIntegration("AskInteg", agent_fn),
            authorizer=jwt_authorizer,
        )
        _gaps_integ = apigwv2_int.HttpLambdaIntegration("GapsInteg", gaps_fn)
        auth_api.add_routes(
            path="/api/gaps",
            methods=[apigwv2.HttpMethod.GET],
            integration=_gaps_integ,
            authorizer=jwt_authorizer,
        )
        # POST /api/gaps/remediate — the Pontos Cegos "Remediar" action (issue #45).
        auth_api.add_routes(
            path="/api/gaps/remediate",
            methods=[apigwv2.HttpMethod.POST],
            integration=_gaps_integ,
            authorizer=jwt_authorizer,
        )
        # GET /api/feed — per-tenant scoped feed (issue #48).
        auth_api.add_routes(
            path="/api/feed",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_int.HttpLambdaIntegration("FeedInteg", feed_api_fn),
            authorizer=jwt_authorizer,
        )
        CfnOutput(self, "AuthApiUrl", value=auth_api.api_endpoint)

        # Cut over /api/ask to the JWT-verified HTTP API, SAME ORIGIN via CloudFront.
        # The dashboard now POSTs same-origin `/api/ask` with `Authorization: Bearer
        # <id_token>`; CloudFront forwards it (Authorization included, Host stripped) to
        # the HTTP API, whose Cognito authorizer does the crypto before the agent Lambda
        # ever runs. No edge basic-auth on this behavior (the JWT is the gate), and no
        # origin secret. Registered BEFORE the /api/* catch-all (insertion order).
        # Point the CloudFront origin at the HTTP API by its STABLE regional domain
        # rather than the `auth_api.http_api_id` token. Referencing the token would make
        # the distribution DependsOn the API, and because the Cognito app client's OAuth
        # callback URL references the distribution's own domain, that closes a CFN
        # circular dependency (distribution -> API -> authorizer -> client -> distribution).
        # The API id is stable for the life of the HTTP API; override via env for a fresh
        # account/region (read the AuthApiUrl output after the first create-only deploy).
        _auth_api_id = os.environ.get("ONCA_AUTH_API_ID", "azml8kx82k")
        api_domain = f"{_auth_api_id}.execute-api.{self.region}.amazonaws.com"
        _api_origin = cf_origins.HttpOrigin(api_domain)
        for _pat in ("/api/ask*", "/api/gaps*", "/api/feed*"):  # #45/#48: JWT API paths
            distribution.add_behavior(
                _pat,
                _api_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
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
        seed_task = sfn_tasks.LambdaInvoke(
            self,
            "SwotSeedTask",
            lambda_function=seed_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.swot_seed",
        )
        seed_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        maintenance_task = sfn_tasks.LambdaInvoke(
            self,
            "SwotMaintenanceTask",
            lambda_function=maintenance_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.swot_maintenance",
        )
        maintenance_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        tows_task = sfn_tasks.LambdaInvoke(
            self,
            "TowsTask",
            lambda_function=tows_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.tows",
        )
        tows_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        porter_task = sfn_tasks.LambdaInvoke(
            self,
            "PorterTask",
            lambda_function=porter_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.porter",
        )
        porter_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        pestle_task = sfn_tasks.LambdaInvoke(
            self,
            "PestleTask",
            lambda_function=pestle_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.pestle",
        )
        pestle_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        ansoff_task = sfn_tasks.LambdaInvoke(
            self,
            "AnsoffTask",
            lambda_function=ansoff_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.ansoff",
        )
        ansoff_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        bcg_task = sfn_tasks.LambdaInvoke(
            self,
            "BcgTask",
            lambda_function=bcg_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.bcg",
        )
        bcg_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        four_corners_task = sfn_tasks.LambdaInvoke(
            self,
            "FourCornersTask",
            lambda_function=four_corners_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.four_corners",
        )
        four_corners_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        seven_s_task = sfn_tasks.LambdaInvoke(
            self,
            "SevenSTask",
            lambda_function=seven_s_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.seven_s",
        )
        seven_s_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        autoapprove_task = sfn_tasks.LambdaInvoke(
            self,
            "AutoApproveTask",
            lambda_function=autoapprove_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.autoapprove",
        )
        autoapprove_task.add_retry(
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
        qsa_task = sfn_tasks.LambdaInvoke(
            self,
            "WatchlistQsaTask",
            lambda_function=qsa_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.watchlist_qsa",
        )
        qsa_task.add_retry(
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
        predictive_task = sfn_tasks.LambdaInvoke(
            self,
            "PredictiveTask",
            lambda_function=predictive_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.predictive",
        )
        predictive_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(15),
            backoff_rate=2.0,
        )
        ecosystem_task = sfn_tasks.LambdaInvoke(
            self,
            "EcosystemTask",
            lambda_function=ecosystem_fn,
            payload=sfn.TaskInput.from_object({}),
            result_path="$.ecosystem",
        )
        ecosystem_task.add_retry(
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

        # Parallelised pipeline (issue #10). Every task takes an empty payload and
        # coordinates ONLY through S3, so the only ordering constraints are data
        # dependencies. Sequential prefix (ingest → feature → synth) produces the
        # digest/features/narratives; then all axis + framework detectors run
        # concurrently (they read features + this run's narratives, write disjoint
        # outputs); FeedTask aggregates everything last. The critical path is the
        # SWOT belief chain + framework fan-out, so wall-clock ≈ that branch, not the
        # sum of ~24 steps. Fan-out concurrency is covered by each task's retry.
        frameworks_parallel = (
            sfn.Parallel(self, "Frameworks", result_path=sfn.JsonPath.DISCARD)
            .branch(tows_task)
            .branch(porter_task)
            .branch(pestle_task)
            .branch(ansoff_task)
            .branch(bcg_task)
            .branch(four_corners_task)
            .branch(seven_s_task)
        )
        # SWOT store is sequential (build → reconcile → seed → maintenance); the
        # framework drafters read it, then auto-approval folds their proposals in.
        swot_branch = (
            swot_task.next(reconcile_task).next(seed_task).next(maintenance_task)
            .next(frameworks_parallel).next(autoapprove_task)
        )
        # QSA (Receita ownership) feeds the operatives person-graph.
        qsa_branch = qsa_task.next(operatives_task)

        # Phase 1 — belief feeders. SwotTask builds beliefs from THIS run's axis
        # narratives that carry a swot_hint (comparative/cohort/thematic) or derive
        # S/W (longitudinal/silence) — swot_store._belief_from_narrative. So these
        # five must finish BEFORE the SWOT branch, or SWOT (and the frameworks over
        # it) miss this run's hints (they'd only land next run via the 90d window).
        # They are independent of each other, so run them concurrently.
        belief_axes = (
            sfn.Parallel(self, "BeliefAxes", result_path=sfn.JsonPath.DISCARD)
            .branch(silence_task)
            .branch(longitudinal_task)
            .branch(comparative_task)
            .branch(thematic_task)
            .branch(cohort_task)
        )
        # Phase 2 — the SWOT+frameworks chain runs concurrently with the detectors
        # that neither feed SWOT nor are read by it (regulatory, threads, behavioral,
        # relational, qsa→operatives, predictive, ecosystem).
        detectors = (
            sfn.Parallel(self, "Detectors", result_path=sfn.JsonPath.DISCARD)
            .branch(swot_branch)
            .branch(regulatory_task)
            .branch(threads_task)
            .branch(behavioral_task)
            .branch(relational_task)
            .branch(qsa_branch)
            .branch(predictive_task)
            .branch(ecosystem_task)
        )
        pipeline = sfn.StateMachine(
            self,
            "OncaPipeline",
            definition_body=sfn.DefinitionBody.from_chainable(
                ingest_task.next(feature_task)
                .next(synth_task)
                .next(belief_axes)
                .next(detectors)
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
# AWS-native GitOps CI/CD (issue #6) — its own stack so the pipeline that deploys
# the app stack is not part of it. Deploy once: `cdk deploy OncaCicdStack`.
from cicd import OncaCicdStack  # noqa: E402  (local module, after app-stack def)

OncaCicdStack(app, "OncaCicdStack")
app.synth()
