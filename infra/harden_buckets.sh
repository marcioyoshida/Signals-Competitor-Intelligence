#!/usr/bin/env bash
# Phase A IP hardening for the data buckets (ADR 002 §6).
#
# onca-digests / onca-raw predate the CDK stack and are imported by name
# (s3.Bucket.from_bucket_name), so CDK does not manage their resource policy,
# logging, or lifecycle. This script is their IaC-equivalent. Idempotent: safe
# to re-run. All operations are additive/config changes — none delete a bucket.
#
# Applied (2026-08-18):
#   1. Bucket policy on data buckets: Deny non-TLS  +  Deny cross-account
#      (aws:PrincipalAccount != this account). All legit readers are in-account
#      Lambda/Bedrock roles, so this cannot lock out the pipeline (verified: the
#      feed-builder Lambda still reads digests after applying).
#   2. S3 server access logging -> a locked-down access-logs bucket.
#   3. Account-level Block Public Access (account-wide guardrail).
#   4. Lifecycle: expire onca-raw objects after 180 days (raw corpus only;
#      NOT digests/narratives). Forward-looking — nothing is old enough to
#      delete at apply time.
#
# NOT here (need a decision — see ADR 002 §6):
#   - SSE-KMS CMK on the data buckets (adds kms:Decrypt as a second gate).
#   - Per-role principal allowlist (tighter than same-account; needs exact
#     principal enumeration + testing — the same-account Deny below is the safe
#     first cut).
set -euo pipefail

ACCOUNT="${ONCA_ACCOUNT:-668449743071}"
DATA_BUCKETS=("onca-digests-${ACCOUNT}" "onca-raw-${ACCOUNT}")
RAW_BUCKET="onca-raw-${ACCOUNT}"
LOG_BUCKET="onca-s3-access-logs-${ACCOUNT}"
RAW_RETENTION_DAYS="${ONCA_RAW_RETENTION_DAYS:-180}"
REGION="${AWS_REGION:-us-east-1}"

data_bucket_policy() {  # $1 = bucket
  cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::$1","arn:aws:s3:::$1/*"],"Condition":{"Bool":{"aws:SecureTransport":"false"}}},
 {"Sid":"DenyCrossAccount","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::$1","arn:aws:s3:::$1/*"],"Condition":{"StringNotEquals":{"aws:PrincipalAccount":"$ACCOUNT"}}}
]}
JSON
}

echo ">> [3] Account-level Block Public Access"
aws s3control put-public-access-block --account-id "$ACCOUNT" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo ">> [2] Locked-down access-logs bucket: $LOG_BUCKET"
if ! aws s3api head-bucket --bucket "$LOG_BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$LOG_BUCKET" --region "$REGION" >/dev/null
fi
aws s3api put-public-access-block --bucket "$LOG_BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$LOG_BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-policy --bucket "$LOG_BUCKET" --policy "$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"S3ServerAccessLogs","Effect":"Allow","Principal":{"Service":"logging.s3.amazonaws.com"},"Action":"s3:PutObject","Resource":"arn:aws:s3:::$LOG_BUCKET/*","Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT"}}},
 {"Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::$LOG_BUCKET","arn:aws:s3:::$LOG_BUCKET/*"],"Condition":{"Bool":{"aws:SecureTransport":"false"}}}
]}
JSON
)"

for B in "${DATA_BUCKETS[@]}"; do
  echo ">> [1] Policy (TLS-only + block-cross-account) + [2] access logging: $B"
  aws s3api put-bucket-policy --bucket "$B" --policy "$(data_bucket_policy "$B")"
  PFX="${B%-$ACCOUNT}/"
  aws s3api put-bucket-logging --bucket "$B" --bucket-logging-status \
    "{\"LoggingEnabled\":{\"TargetBucket\":\"$LOG_BUCKET\",\"TargetPrefix\":\"$PFX\"}}"
done

echo ">> [4] Lifecycle: expire $RAW_BUCKET objects after ${RAW_RETENTION_DAYS}d (raw corpus only)"
aws s3api put-bucket-lifecycle-configuration --bucket "$RAW_BUCKET" \
  --lifecycle-configuration "$(cat <<JSON
{"Rules":[{"ID":"expire-raw-corpus-${RAW_RETENTION_DAYS}d","Status":"Enabled","Filter":{"Prefix":""},"Expiration":{"Days":${RAW_RETENTION_DAYS}},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}}]}
JSON
)"
echo ">> Done."
