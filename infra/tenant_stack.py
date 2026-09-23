"""ADR 016 addendum (2026-09-22), Decision 4, step 1: the in-account (Sovereign,
#49) CDK stack — a SEPARATE deployable, not a mode of `OncaPrototypeStack`.

**What this is, and what it deliberately is NOT yet.** This is the account-
boundary shell: the resources a Sovereign tenant's own AWS account is allowed to
hold, parameterized, synthesizable, and — critically — provably free of the one
thing that must never cross into a tenant account (`onca-entities`, the registry,
ADR 005 §1/§2). It is NOT yet a working pipeline: the ingest/synth/feed Lambdas
and the dashboard are explicitly deferred (steps 2-3 below), because porting them
safely depends on work this stack alone can't do:

  - `src/ingest/lambda_port.py` (today's single ingest Lambda) runs entity
    DISCOVERY — `ONCA_ENTITY_DISCOVERY`, `ONCA_NER_HARVEST`,
    `ONCA_SEARCH_EXPANSION`, the Receita bulk fetch — against `entities_table`
    directly. Deploying that Lambda unchanged into a tenant account would
    silently re-run wholesale discovery in-account and let the tenant's local
    table grow into its own shadow registry — exactly what ADR 005 §2 rejects
    ("Discovery stays central... does NOT re-run wholesale diff to build its own
    registry"). It cannot be ported until `src/synth/entities.py` has the
    resolution-mode seam (Decision 4 step 3: internal registry vs a live
    `POST /resolve` call) AND the vendor-side `/resolve` API exists (Decision 3,
    also not built — see the addendum's Finding 0.1). Wiring it in today, even as
    a "temporary" stopgap, would ship the moat's mechanism into a tenant account.
  - `src/ingest/tenant_s3.py` (the private-S3 lens, ADR 005 §3) doesn't exist yet
    — Decision 4 step 2, tracked separately, not started here.
  - The dashboard is the vendor's multi-tenant `site/` bundle; a Sovereign
    tenant's own single-tenant dashboard is a real design question (auth model,
    since there's no multi-tenant boundary to enforce inside a tenant's own
    perimeter) deferred to when steps 2-3 land real content to serve.

So this file stands up: the tenant's OWN entity cache + engagement table (the
ADR 005 §2 "encounter-only TTL cache", explicitly NOT a registry replica), the
tenant's OWN KB (S3 Vectors + Bedrock, mirroring the vendor's pattern exactly —
see `OncaKnowledgeBase` in `infra/app.py`), the tenant's OWN raw/digests buckets,
and the IAM role identity that Decision 3's onboarding step registers against the
vendor's `/resolve` API trust policy. Every one of these is safe to hold in a
tenant account TODAY, independent of the unbuilt `/resolve` API or the entities
seam — which is exactly why they're the right first slice.

**Not deployed anywhere by this commit.** There is no real tenant AWS account to
deploy into yet. Validate with `cdk synth --app "python infra/tenant_app.py"` —
deploying this into the VENDOR's own account would create a confusing, costly,
pointless duplicate with no tenant to serve it, and deploying "somewhere" isn't a
real target until a design partner is actually being onboarded onto Sovereign.
"""
from __future__ import annotations

from aws_cdk import CfnOutput, RemovalPolicy, Stack, Tags
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3vectors as s3vectors

# Kept identical to infra/app.py's vendor stack — the embedding model/dimension
# are a corpus-format decision, not a per-account one; the tenant's KB must match
# the vendor's so a future cross-plane comparison (or migration) isn't blocked by
# a silent dimension mismatch.
EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
VECTOR_DIMENSION = 1024

# ADR 005 §2: cache only entities this tenant has actually encountered, TTL'd,
# never a bulk pull. 30 days is generous enough that a weekly-cadence synth run
# doesn't re-resolve the same recurring entities every week, short enough that a
# registry correction (a curator fixing an alias/industry vendor-side) reaches
# every Sovereign tenant within a month rather than being cached stale for years.
ENTITY_CACHE_TTL_DAYS = 30

# ADR 016 addendum Decision 4 step 4: this deployable's pinned version. Tracks
# `src/synth/resolver.py`'s RESOLVE_CONTRACT_VERSION 1:1 — bump both together,
# only when the `/resolve` request/response shape changes (ADR 005 §Costs:
# "the resolve contract is the compatibility boundary" — that's the thing this
# version actually gates, not internal CDK refactors). Full policy — what
# counts as breaking, the support window, the upgrade procedure — lives in
# docs/tenant-stack-versioning.md, not duplicated here.
TENANT_STACK_VERSION = "1.0.0"


class OncaTenantStack(Stack):
    """The in-account half of a Sovereign deployment (ADR 005/016, #49).

    Parameterized via CDK context (not CfnParameter): `tenant_id` identifies the
    deployment for tagging/naming, matching the pattern `dashboard_credentials()`
    /`google_client_id` already use in the vendor stack (context, resolved at
    synth time, not a runtime parameter a Lambda reads). A real onboarding would
    set these via `-c tenant_id=... -c vendor_resolve_api_url=...` on `cdk deploy`
    inside the TENANT's own account/credentials — never the vendor's.
    """

    def __init__(self, scope: object, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        tenant_id = self.node.try_get_context("tenant_id") or "sample-tenant"
        # The vendor's /resolve endpoint this tenant's synth will call once
        # Decision 3/entities.py's seam exist. Recorded now (as a tag + output)
        # so it's visible in the stack from day one, even though nothing calls it
        # yet — the onboarding conversation ("which endpoint, which role") should
        # happen once, at first deploy, not be bolted on silently later.
        vendor_resolve_api_url = self.node.try_get_context("vendor_resolve_api_url") or ""

        Tags.of(self).add("onca-tenant-id", tenant_id)
        Tags.of(self).add("onca-plane", "marketplace")  # ADR 016 addendum Decision 2
        Tags.of(self).add("onca-tenant-stack-version", TENANT_STACK_VERSION)
        CfnOutput(self, "TenantStackVersion", value=TENANT_STACK_VERSION)

        # --- The encounter-only entity cache + this tenant's own engagement log ----
        # ONE table, matching the vendor's own single-table convention exactly
        # (`onca-entities`: ENT#/ALIAS#/CNPJ# rows AND, per `engagement_log.py`,
        # `ENGAGEMENT#<id>` rows too — record_engagement() reads/writes through
        # `entity_registry._table()`, the SAME table as the registry). That
        # co-location is why Decision 1's "telemetry-off falls out of locality"
        # claim holds precisely, not just approximately: `record_engagement` is
        # UNCHANGED code that, pointed at THIS table via ONCA_ENTITIES_TABLE, has
        # no path back to the vendor's table at all — there is no cross-account
        # DynamoDB access to misconfigure into working.
        #
        # This table is NEVER `onca-entities` (the registry) — it holds only
        # resolve results this tenant has actually encountered (TTL'd) plus its
        # own engagement events. A tenant with zero cached entities has zero
        # registry knowledge; the registry itself never leaves the vendor account.
        entity_cache_table = dynamodb.Table(
            self,
            "OncaTenantEntityCache",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )
        CfnOutput(self, "TenantEntityCacheTable", value=entity_cache_table.table_name)

        # This tenant's own ADR-018-style curation/audit journal — for THEIR
        # local curation (SWOT vetting, review-queue resolution, decision logs)
        # once steps 2-3 land synth/feed here. Not registry curation (there is
        # none to curate; the registry stays central) — this is the audit trail
        # for whatever this tenant's OWN dashboard lets an in-account curator do.
        curation_log_table = dynamodb.Table(
            self,
            "OncaTenantCurationLog",
            partition_key=dynamodb.Attribute(name="entity_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="ts", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
        )
        CfnOutput(self, "TenantCurationLogTable", value=curation_log_table.table_name)

        # --- This tenant's own raw/digests storage — never the vendor's buckets -----
        raw_bucket = s3.Bucket(
            self,
            "OncaTenantRawBucket",
            removal_policy=RemovalPolicy.RETAIN,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )
        digests_bucket = s3.Bucket(
            self,
            "OncaTenantDigestsBucket",
            removal_policy=RemovalPolicy.RETAIN,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )
        CfnOutput(self, "TenantRawBucket", value=raw_bucket.bucket_name)
        CfnOutput(self, "TenantDigestsBucket", value=digests_bucket.bucket_name)

        # --- This tenant's own KB — S3 Vectors + Bedrock, mirroring the vendor's --
        # pattern in infra/app.py's OncaKnowledgeBase exactly. Per-tenant Bedrock/KB
        # compute landing on the TENANT's own account (not the vendor's ~$100/mo
        # prototype ceiling) is one of ADR 005's stated positives — this block is
        # where that actually happens.
        vector_bucket = s3vectors.CfnVectorBucket(
            self,
            "OncaTenantVectorBucket",
            vector_bucket_name=f"onca-tenant-{tenant_id}-vectors",
        )
        vector_index = s3vectors.CfnIndex(
            self,
            "OncaTenantVectorIndex",
            vector_bucket_name=vector_bucket.vector_bucket_name,
            index_name="onca-tenant-corpus",
            data_type="float32",
            dimension=VECTOR_DIMENSION,
            distance_metric="cosine",
            # See infra/app.py's identical block: the Bedrock-reserved metadata
            # keys must stay non-filterable or a full-text chunk blows the 2048B
            # filterable-metadata cap and silently fails to index.
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=[
                    "AMAZON_BEDROCK_METADATA",
                    "AMAZON_BEDROCK_TEXT",
                ]
            ),
        )
        vector_index.add_dependency(vector_bucket)

        kb_role = iam.Role(
            self,
            "OncaTenantKnowledgeBaseRole",
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
            "OncaTenantKnowledgeBase",
            name="onca-tenant-corpus",
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
            "OncaTenantKnowledgeBaseDataSource",
            knowledge_base_id=knowledge_base.attr_knowledge_base_id,
            name="onca-tenant-raw-corpus",
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

        # --- The /resolve-caller identity (ADR 016 addendum Decision 3) ------------
        # This role is the onboarding ARTIFACT: it exists here, in the tenant's own
        # account, and its ARN is the one thing exchanged with the vendor to grant
        # this specific tenant's future synth Lambda access to POST /resolve — a
        # per-tenant resource-policy trust statement on the vendor side, not a
        # shared API key (Decision 3's explicit rejection of that shape: one leak
        # must not compromise every Sovereign tenant, and revocation must be a
        # single unambiguous lever — detaching this role's trust grant vendor-side).
        # No Lambda assumes it yet (the synth Lambda that would doesn't exist until
        # step 3's resolution-mode seam lands) — but standing up the IDENTITY now,
        # ahead of anything using it, is what makes "give the vendor this ARN" a
        # concrete first onboarding step rather than a future unknown.
        resolve_caller_role = iam.Role(
            self,
            "OncaResolveCallerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Assumed by this tenant's synth Lambda (not yet built) to call the "
                "vendor's POST /resolve. Its ARN is registered vendor-side as a "
                "per-tenant trust statement — see ADR 016 addendum Decision 3."
            ),
        )
        CfnOutput(self, "ResolveCallerRoleArn", value=resolve_caller_role.role_arn)
        CfnOutput(
            self,
            "VendorResolveApiUrl",
            value=vendor_resolve_api_url or "(not set — pass -c vendor_resolve_api_url=...)",
        )
        CfnOutput(self, "TenantId", value=tenant_id)
