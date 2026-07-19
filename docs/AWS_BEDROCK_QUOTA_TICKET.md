# Bedrock embedding quota — Stage A unblock (my2027)

**Account:** `668449743071` (alias my2027)  
**Region:** `us-east-1`  
**Date opened (self-service):** 2026-07-19  
**Why:** Stage A Knowledge Base ingestion (`StartIngestionJob` → Titan Embed
Text v2) fails with **429 Too many requests** because on-demand embedding
RPM is **0** for every embed model in this account.

## Already submitted (Service Quotas API)

| Request ID | Quota | Code | Desired | Status |
|---|---|---|---|---|
| `5e74c6f330c84cc8be6cec494978d3adsWn5hNLN` | Global cross-region RPM Cohere Embed V4 | `L-7089DC7D` | **3000** | PENDING |
| `77ea5271c78a4280880c6b3892708991MieiIJO5` | Global cross-region TPM Cohere Embed V4 | `L-02DFBB76` | **400000** | PENDING |

Check status:

```bash
aws service-quotas get-requested-service-quota-change \
  --request-id 5e74c6f330c84cc8be6cec494978d3adsWn5hNLN \
  --region us-east-1 --profile my2027

aws service-quotas list-requested-service-quota-change-history \
  --service-code bedrock --region us-east-1 --profile my2027 \
  --query 'RequestedQuotas[?Status==`PENDING` || Status==`CASE_OPENED`]'
```

**Note:** Account applied value was `0` while AWS default is `2000` RPM /
`300000` TPM for those cross-region Embed V4 quotas. Requesting **above**
the published default is required (AWS rejects “request more than default”
if you ask for ≤ default).

## Still needed (not self-service)

**Titan Text Embeddings V2** on-demand RPM (`L-26C560CE`) is the model the
KB is wired to (`amazon.titan-embed-text-v2:0`). Quota code shows:

- Applied account value: **0.0**
- AWS default: **6000**
- **Adjustable: false** via Service Quotas API

This needs an **AWS Support case** (Support plan was **not** enabled on
2026-07-19 — `SubscriptionRequiredException` on Support APIs).

### Enable Support then open a case

1. Console → **Support Center** → enroll at least **Developer** support
   (or Business if you already plan design partners).
2. Create case → **Service limit increase** / **Account and billing** /
   **Technical** → Service: **Amazon Bedrock**.
3. Paste the template below.

### Support case template (copy/paste)

**Subject:** Enable on-demand throughput for Amazon Titan Text Embeddings V2 (account 668449743071)

**Body:**

```
Account ID: 668449743071
Region: us-east-1
Use case: Onça competitive-intelligence prototype (AWS Marketplace path).
We run a Bedrock Knowledge Base (storageConfiguration type S3_VECTORS)
with embedding model amazon.titan-embed-text-v2:0.

Problem:
- bedrock-agent StartIngestionJob fails with 429 Too many requests on
  amazon.titan-embed-text-v2:0.
- service-quotas shows On-demand model inference requests per minute for
  Amazon Titan Text Embeddings V2 (L-26C560CE) = 0.0 on this account.
- Quota is marked Adjustable: false, so we cannot raise it via
  request-service-quota-increase.
- Model access appears granted (list-foundation-models includes embed
  models); the gap is on-demand inference throughput, not model access.

Request:
1. Provision default (or higher) on-demand RPM/TPM for
   amazon.titan-embed-text-v2:0 in us-east-1 so Knowledge Base ingestion
   can complete.
2. If Titan V2 on-demand cannot be enabled, please advise the supported
   path to use Cohere Embed V4 via a cross-region inference profile with
   Bedrock Managed Knowledge Base + S3 Vectors in this account.

We already filed self-service increases for Cohere Embed V4 global
cross-region RPM (L-7089DC7D → 3000) and TPM (L-02DFBB76 → 400000).

Expected volume: once-daily ingestion of tens–low hundreds of short
regulatory/competitor text documents (prototype cost ceiling ~$100/mo).
```

## After any quota is approved — verify Stage A end-to-end

```bash
export AWS_PROFILE=my2027 AWS_DEFAULT_REGION=us-east-1
# Confirm non-zero RPM
aws service-quotas get-service-quota --service-code bedrock \
  --quota-code L-26C560CE --region us-east-1 \
  --query 'Quota.Value'

# Re-run ingest Lambda (or StartIngestionJob) and wait for COMPLETE
# Then:
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id "$ONCA_KB_ID" \
  --retrieval-query text="BCB normativo Pix" \
  --region us-east-1
```

Success criteria: `get-ingestion-job` → `COMPLETE`; Retrieve returns a
chunk with metadata `url` / `date` matching a known raw object under
`s3://onca-raw-668449743071/`.

## Optional follow-up (if Embed V4 cross-region is approved first)

Evaluate switching the KB embedding model ARN from Titan V2 to a **Cohere
Embed V4 cross-region inference profile** (requires CDK change + vector
dimension check — do not switch dimensions without recreating the index).
Document any such change in `docs/2026-07-12-phase2-stage-a-knowledge-base.md`.
