# Phase 2, Stage A: Bedrock Knowledge Base (S3 Vectors) — 2026-07-12

## What this is

Phase 1/1.5 (data spine: BCB normativos, CVM fund filings, BCB IF.data
market share, diffed daily via DynamoDB) is done and validated live. This is
the first slice of Phase 2 — the correlation/synthesis product described in
CLAUDE.md as "This correlation IS the product." Stage A builds the corpus
and retrieval layer; it does not yet produce any narratives.

## What was built

- `src/ingest/raw_writer.py` — writes each new (already-diffed) regulatory
  or competitor document as a `{source}/{id}.txt` object plus a
  `.metadata.json` sidecar (Bedrock's documented per-object metadata
  convention) to `onca-raw-{account}`. Market share is aggregate numeric
  data, not a citable document, so it's excluded from the corpus — it'll be
  passed as direct prompt context in Stage B instead.
- `src/ingest/lambda_port.py` — after diffing, calls `raw_writer` and then
  (only if something new was written) `bedrock-agent.start_ingestion_job`
  to sync the new documents into the Knowledge Base. Both steps degrade
  gracefully on failure, matching the existing pattern for every other
  external call in this Lambda.
- `infra/app.py` — provisions an S3 Vectors bucket + index (`float32`,
  1024 dimensions matching `amazon.titan-embed-text-v2:0`, cosine
  distance), a self-managed Bedrock Knowledge Base (`storageConfiguration.
  type=S3_VECTORS` — the CLAUDE.md-mandated choice over OpenSearch
  Serverless, which has a ~$345+/mo idle floor), and a Data Source pointed
  at `onca-raw-{account}` with fixed-size chunking (~300 tokens — these are
  short regulatory/fund records). CDK 2.261.0 has no L2 constructs for
  either S3 Vectors or Bedrock Knowledge Base yet, so this is hand-wired L1
  (`Cfn*`) throughout, same pattern already used for the rest of this stack.

## Decision on file: AgentCore Runtime deferred

CLAUDE.md names "Bedrock AgentCore" as the reasoning layer. Stage B (not
built yet — the actual correlation/synthesis logic) will call Bedrock's
Retrieve + Converse APIs directly from a plain Lambda instead of deploying
a container-hosted AgentCore Runtime. Reasoning: this is a once-daily batch
job over tens of documents, with no session state and no interactivity.
AgentCore Runtime buys session management and a hosted execution
environment — neither used here — at the cost of real new ops surface
(container image, ECR, runtime artifact versioning) that breaks this repo's
"small pure fetch functions, Lambda-portable" convention for no functional
gain. Revisit true AgentCore Runtime if/when an interactive dashboard agent
(Phase 3+) needs real session semantics.

## What's still missing (Stage B)

The KB can now be populated and queried, but nothing yet reads it. Stage B
is a second Lambda that: retrieves recent regulatory items, uses a cheap
model (Haiku/Nova) to classify/route candidates, retrieves related
competitor documents from the KB, and uses a stronger model (Sonnet) to
synthesize a flagged narrative — with a citation guardrail that drops any
URL not present in the actual retrieved set, enforcing CLAUDE.md's "no
uncited claims" rule in code, not just prompt instruction.

## Verification

`cdk diff` before deploy showed only new resources (vector bucket/index, KB,
data source, IAM role, Lambda env vars + one new IAM statement) — no
changes to existing Phase 1.5 infra. Deployed cleanly (13/13 resources).

Live-validated up through corpus population:
- Reset the `bcb_normativos` DynamoDB diff state and re-invoked the ingest
  Lambda to force real new documents through the pipeline (49 normativos).
- Confirmed 98 objects (49 `.txt` + 49 `.metadata.json`) landed in
  `onca-raw-{account}/BCB/`, with correct chunk-ready text bodies and
  metadata sidecars carrying real citation URLs
  (`s3 cp .../bcb:Comunicado:45507.txt.metadata.json` inspected directly).

**Blocked**: `StartIngestionJob` (called correctly, confirmed via
CloudWatch logs) fails with a `429 Too many requests` from
`amazon.titan-embed-text-v2:0` — `aws service-quotas list-service-quotas
--service-code bedrock` shows **every** embedding model's on-demand
requests-per-minute quota at `0.0` in this account (Titan v1/v2, Titan
Multimodal, Cohere Embed English/Multilingual/V4, Nova Multimodal), most
marked `Adjustable: false`. This is model access (already granted, per
`list-foundation-models`) being distinct from on-demand throughput
provisioning — a known gap for freshly-created accounts. Confirmed this
isn't a code/config bug: the KB, data source, IAM role, and ingestion call
are all correctly wired; AWS is refusing the embedding call at the account
level.

One wrinkle worth a follow-up: requesting an increase on the one
`Adjustable: true` quota found (`Global cross-region model inference
requests per minute for Cohere Embed V4`, code `L-7089DC7D`) returned "you
must request more than the default value of 2000" — meaning the *effective*
default may actually be 2000 for that specific cross-region quota, and the
`0.0` reported by `list-service-quotas` might not reflect the real
enforced limit for cross-region inference profiles. Not chased further this
session — switching the KB to a cross-region inference profile ARN is a
real (not yet made) config change, not something to do speculatively.

**Next step before Stage B**: either open an AWS Support case to raise the
Titan Embed Text v2 on-demand quota (the standard path for a genuinely
zero, non-adjustable quota), or investigate the Cohere Embed V4
cross-region signal further. Once ingestion completes, finish the
originally-planned check: `get-ingestion-job` reports `COMPLETE`, then a
manual `bedrock-agent-runtime retrieve` call against the KB should return
a chunk whose metadata (`url`, `date`, `doc_type`) matches the source
document — proving citations survive the round trip before Stage B is
built on top of them.
