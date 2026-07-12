#!/usr/bin/env bash
# Onça — AWS account bootstrap for my2027 (668449743071)
# Run once from your machine. Idempotent where possible.
set -euo pipefail

ACCOUNT_ID="668449743071"
PROFILE="my2027"
REGION="${AWS_REGION:-us-east-1}"

echo "── 1. Verify credentials point at the right account"
CALLER=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
if [ "$CALLER" != "$ACCOUNT_ID" ]; then
  echo "ERROR: profile '$PROFILE' resolves to account $CALLER, expected $ACCOUNT_ID"
  echo "Configure it first:  aws configure sso --profile $PROFILE   (or aws configure)"
  exit 1
fi
echo "OK — $PROFILE → $ACCOUNT_ID"

echo "── 2. CDK bootstrap (required once per account+region)"
npx cdk bootstrap "aws://$ACCOUNT_ID/$REGION" --profile "$PROFILE"

echo "── 3. Baseline S3 buckets (raw landing + digests)"
for BUCKET in "onca-raw-$ACCOUNT_ID" "onca-digests-$ACCOUNT_ID"; do
  if aws s3api head-bucket --bucket "$BUCKET" --profile "$PROFILE" 2>/dev/null; then
    echo "exists: $BUCKET"
  else
    aws s3api create-bucket --bucket "$BUCKET" --profile "$PROFILE" --region "$REGION"
    aws s3api put-public-access-block --bucket "$BUCKET" --profile "$PROFILE" \
      --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    echo "created: $BUCKET (public access blocked)"
  fi
done

echo "── 4. Bedrock model access reminder (manual, console-only)"
echo "Open: https://$REGION.console.aws.amazon.com/bedrock/home?region=$REGION#/modelaccess"
echo "Request access to: Claude Haiku (routing/classification) + one stronger"
echo "Claude model (synthesis) + Titan/Nova embeddings."

echo "── 5. Cost guardrail — budget alarm at prototype ceiling"
aws budgets create-budget --profile "$PROFILE" --account-id "$ACCOUNT_ID" \
  --budget '{"BudgetName":"onca-prototype-ceiling","BudgetLimit":{"Amount":"100","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST"}' \
  --notifications-with-subscribers '[{"Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":80},"Subscribers":[{"SubscriptionType":"EMAIL","Address":"REPLACE_WITH_YOUR_EMAIL"}]}]' \
  2>/dev/null && echo "budget created" || echo "budget exists or needs email set — edit REPLACE_WITH_YOUR_EMAIL"

echo
echo "Done. Next: cd infra && npx cdk deploy (once the CDK app exists)."
