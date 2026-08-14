# Bedrock embedding quota — Stage A unblock (my2027)

**Account:** `668449743071` (alias my2027)  
**Region:** `us-east-1`
**Date opened (self-service):** 2026-07-19
**Status:** ✅ **RESOLVED** - Titan V2 quota approved on 2026-08-10

## Summary of Approvals

### Cohere Embed V4 Cross-Region Quotas (Self-Service)

| Request ID | Quota | Code | Desired | Actual | Status |
|---|---|---|---|---|---|
| `5e74c6f330c84cc8be6cec494978d3adsWn5hNLN` | Global cross-region RPM Cohere Embed V4 | `L-7089DC7D` | **3000** | **20** | ✅ CASE_CLOSED (2026-08-05) |
| `77ea5271c78a4280880c6b3892708991MieiIJO5` | Global cross-region TPM Cohere Embed V4 | `L-02DFBB76` | **400000** | **300000** | ✅ CASE_CLOSED (2026-08-05) |

Note: The RPM quota was approved at 20 instead of the requested 3000, but this is still above the AWS default of 10 RPM.

### Titan Text Embeddings V2 Quota (AWS Support Case Required)

| Quota | Code | Desired | Actual | Status |
|---|---|---|---|---|
| On-demand RPM Titan V2 | `L-26C560CE` | **6000** (default) | **60** | ✅ **APPROVED** (2026-08-10) |

The quota was increased from 0 to 60 RPM, which is sufficient for our prototype needs (tens of documents/day).

## Verification Commands

```bash
export AWS_PROFILE=my2027 AWS_DEFAULT_REGION=us-east-1

# Verify Titan V2 quota
aws service-quotas get-service-quota --service-code bedrock \
  --quota-code L-26C560CE --region us-east-1 \
  --query 'Quota.Value'

# Verify Cohere Embed V4 quotas
aws service-quotas get-service-quota --service-code bedrock \
  --quota-code L-7089DC7D --region us-east-1 \
  --query 'Quota.Value'

aws service-quotas get-service-quota --service-code bedrock \
  --quota-code L-02DFBB76 --region us-east-1 \
  --query 'Quota.Value'
```

## Stage A End-to-End Validation (Next Steps)

Now that the Titan V2 quota is approved, proceed with live validation:

```bash
# Re-run ingest Lambda and wait for COMPLETE
aws lambda invoke \
  --function-name OncaPrototypeStack-OncaLambdaPrototype6DC6C2A9-cEyrm4m90PE0 \
  /tmp/response.json

# Check ingestion job status (get the job ID from Lambda logs)
aws bedrock-agent get-ingestion-job \
  --knowledge-base-id "$ONCA_KB_ID" \
  --job-id "<INGESTION_JOB_ID>"

# Test retrieval once COMPLETE
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id "$ONCA_KB_ID" \
  --retrieval-query text="BCB normativo Pix" \
  --region us-east-1
```

Success criteria: `get-ingestion-job` → `COMPLETE`; Retrieve returns a chunk with metadata `url` / `date` matching a known raw object under `s3://onca-raw-668449743071/`.

## Optional Follow-Up (If Needed)

If we need higher throughput for Cohere Embed V4, we may need to open another support case to request the full 3000 RPM.
