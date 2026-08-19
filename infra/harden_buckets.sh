#!/usr/bin/env bash
# Phase A IP hardening for the data buckets (ADR 002 §6).
#
# onca-digests / onca-raw predate the CDK stack and are imported by name
# (s3.Bucket.from_bucket_name), so CDK does not manage their resource policy or
# logging. This script applies the at-rest controls directly and is the
# IaC-equivalent for those buckets. Idempotent: safe to re-run.
#
# Applied here (all zero-risk, reversible):
#   1. TLS-only bucket policy (Deny when aws:SecureTransport=false).
#   2. S3 server access logging -> a locked-down access-logs bucket.
#
# Deliberately NOT here (need a decision / are account-wide — see ADR 002 §6):
#   - Lifecycle expiration of raw corpus (data-retention decision, deletes data).
#   - Principal-allowlist Deny (needs exact principal enumeration + testing).
#   - Account-level Block Public Access (account-wide; confirm before enabling:
#       aws s3control put-public-access-block --account-id "$ACCOUNT" \
#         --public-access-block-configuration \
#         BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true )
#   - SSE-KMS CMK on the data buckets (adds kms:Decrypt as a second gate).
set -euo pipefail

ACCOUNT="${ONCA_ACCOUNT:-668449743071}"
DATA_BUCKETS=("onca-digests-${ACCOUNT}" "onca-raw-${ACCOUNT}")
LOG_BUCKET="onca-s3-access-logs-${ACCOUNT}"
REGION="${AWS_REGION:-us-east-1}"

tls_only_policy() {  # $1 = bucket
  cat <<JSON
{"Version":"2012-10-17","Statement":[{"Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::$1","arn:aws:s3:::$1/*"],"Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}
JSON
}

echo ">> Ensuring locked-down access-logs bucket: $LOG_BUCKET"
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
  echo ">> Hardening $B"
  aws s3api put-bucket-policy --bucket "$B" --policy "$(tls_only_policy "$B")"
  PFX="${B%-$ACCOUNT}/"
  aws s3api put-bucket-logging --bucket "$B" --bucket-logging-status \
    "{\"LoggingEnabled\":{\"TargetBucket\":\"$LOG_BUCKET\",\"TargetPrefix\":\"$PFX\"}}"
  echo "   TLS-only policy + access logging -> $LOG_BUCKET/$PFX"
done
echo ">> Done."
