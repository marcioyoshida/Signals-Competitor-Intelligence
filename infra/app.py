"""CDK app for the Phase 1.5 Lambda prototype.

This deploys a scheduled Lambda that runs the ingestion prototype and can
later be extended to S3/DynamoDB persistence.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from aws_cdk import App, Duration, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3vectors as s3vectors

EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
VECTOR_DIMENSION = 1024


REPO_ROOT = Path(__file__).resolve().parents[1]
LAMBDA_ASSET = REPO_ROOT / "build" / "lambda"
WATCHLIST_CONFIG = REPO_ROOT / "config" / "watchlist.yaml"


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
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "PYTHONPATH": "/var/task",
                "ONCA_STATE_TABLE": state_table.table_name,
                "ONCA_DIGESTS_BUCKET": digests_bucket.bucket_name,
                "ONCA_LOOKBACK_DAYS": str(watchlist.get("lookback_days", 7)),
                "ONCA_COMPETITORS": ",".join(watchlist.get("competitors", [])),
                "ONCA_RAW_BUCKET": raw_bucket.bucket_name,
                "ONCA_KB_ID": knowledge_base.attr_knowledge_base_id,
                "ONCA_KB_DATA_SOURCE_ID": data_source.attr_data_source_id,
            },
        )
        state_table.grant_read_write_data(func)
        digests_bucket.grant_put(func)
        raw_bucket.grant_put(func)
        func.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:StartIngestionJob"],
                resources=[knowledge_base.attr_knowledge_base_arn],
            )
        )

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
