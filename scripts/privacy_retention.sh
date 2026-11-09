#!/usr/bin/env bash
# Retention limits promised by the privacy policy (onssa.org/docs/privacy.html) for stores
# CDK does not configure. Idempotent; re-run after any deploy that adds Lambdas or log groups.
#   - CloudWatch Logs: every Onça log group expires after 90 days (Lambdas auto-create their
#     groups with "never expire").
#   - CloudFront access logs (IP, URI, referer): 365 days.
#   - S3 server access logs for onca-raw/onca-digests (our own roles only, no user data):
#     5 years in Standard, then deleted (#310). ~10 KB objects: a Glacier step would cost more
#     in transition fees than it saves, and S3 skips objects under 128 KB by default.
# The contact-mail bucket's 365-day rule lives in infra/app.py (OncaContactMailBucket).
set -euo pipefail
LOG_DAYS=90
CF_LOG_BUCKET=${CF_LOG_BUCKET:-oncaprototypestack-oncadashboardcdnloggingbucket53-skms44zxqbo1}
S3_LOG_BUCKET=${S3_LOG_BUCKET:-onca-s3-access-logs-$(aws sts get-caller-identity --query Account --output text)}
S3_LOG_DAYS=1825

aws logs describe-log-groups --query 'logGroups[].[logGroupName,retentionInDays]' --output text |
  awk -v d="$LOG_DAYS" 'tolower($1) ~ /onca/ && $2 != d {print $1}' |
  while read -r g; do
    aws logs put-retention-policy --log-group-name "$g" --retention-in-days "$LOG_DAYS"
    echo "logs: $g -> ${LOG_DAYS}d"
  done

aws s3api put-bucket-lifecycle-configuration --bucket "$CF_LOG_BUCKET" --lifecycle-configuration '{
  "Rules": [{"ID": "privacy-cf-access-365d", "Status": "Enabled",
             "Filter": {"Prefix": "cf-access/"}, "Expiration": {"Days": 365}}]}'
echo "s3: $CF_LOG_BUCKET cf-access/ -> 365d"

aws s3api put-bucket-lifecycle-configuration --bucket "$S3_LOG_BUCKET" --lifecycle-configuration '{
  "Rules": [{"ID": "privacy-s3-access-5y", "Status": "Enabled", "Filter": {"Prefix": ""},
             "Expiration": {"Days": '"$S3_LOG_DAYS"'},
             "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}]}'
echo "s3: $S3_LOG_BUCKET -> ${S3_LOG_DAYS}d"
