# ADR 026 — Execution environment for the Receita CNPJ bulk fetch (#104 unblock)

Status: PROPOSED 2026-09-14. Not yet built — recorded so the decision doesn't get
re-litigated from scratch, and so the two follow-on tickets have a shared rationale to
point at.
Relates to ADR 011 (entity discovery/enrichment — this is Stage 2 of that pipeline),
ADR 018 (curation provenance — the fetch only ever proposes, never auto-creates), and
issue #104 (#14 Stage 2).

## Context

`src/ingest/receita_bulk.py` (`cf3534e`) already ships the real Stage-2 core: parse the
Receita "Estabelecimentos" bulk layout, filter to financial-services CNAE divisions
(64/65/66), map CNAE→industry, dedup against the registry by CNPJ root, and propose the
new ones (ADR 011 §4 — CNPJ-only candidates are review-gated, never auto-created at
scale). It is pure, unit-tested, and table-agnostic.

A live shard-fetch (`a4d1352`) was also already built and live-tested: the dump is not
one 60M-row monolith but 10 numbered `Estabelecimentos{0-9}.zip` shards on a third-party
mirror (the official gov.br path now sits behind an SSO login a Lambda can't complete).
Two live invocations — 240s and then 480s per-source budgets — **both exceeded budget**,
the second running 885s against a single ~326MB shard, nearly the entire 900s Lambda
ceiling. This is not a code defect; Lambda's 15-minute hard limit cannot be raised
further (already tested at max: 1024MB ephemeral storage, 15-minute timeout), and the
mirror's throughput (~380KB/s observed) is simply too slow for a shared, multi-source
ingest Lambda that budgets ~90s per source across dozens of sources in one run.

Live-reverified 2026-09-14 against the mirror:
- Latest dump: `2026-08-09` (cadence is genuinely ~monthly: 04-12 → 05-10 → 06-14 →
  07-12 → 08-09 — no reason to fetch more often than the source publishes).
- `Estabelecimentos{0-9}.zip` AND `Empresas{0-9}.zip` both exist per dump, same 10-shard
  split (`Empresas` also carries `Socios{0-9}.zip`, out of scope here).
- Sizes: `Estabelecimentos1.zip` = 326MB, `Empresas1.zip` = 74MB (Empresas shards run
  roughly 4x smaller). A full monthly pull of both files, all 10 shards each
  (including the larger shard 0, currently excluded), is ≈5.5-6GB.
- At the previously observed mirror throughput, that full pull is roughly 4 hours
  end to end — comfortably outside any Lambda invocation, regardless of tuning.

The project has **no existing ECS/Fargate/Batch/Glue/Athena footprint** — every
pipeline stage today is a Lambda orchestrated by Step Functions (see the pipeline
topology doc). Any option here introduces a new AWS service class.

## Decision

Run the heavy fetch as an **AWS Glue Python Shell job**, scheduled monthly (matching
the mirror's actual update cadence), reusing `receita_bulk.py` unmodified as the
job's core logic.

Why Glue Python Shell over the alternatives:

- **No new infra class to operate.** No VPC, no ECS cluster, no container image/ECR, no
  compute environment/job queue — just a script (S3-hosted, `--extra-py-files` for the
  shared module) + an IAM role, the same operational shape as every existing Lambda.
- **No timeout ceiling problem.** Up to 48h of runtime vs. Lambda's hard 15-minute cap —
  comfortably covers the ~4h estimate with headroom for a slower month or a bigger dump.
- **Trivial cost.** Billed per DPU-second, 1 DPU minimum; ~4 DPU-hours/month × ~$0.44/DPU-hr
  ≈ **~$2/month** — negligible against the project's ~$100/mo total AWS budget.
- **Fits the existing orchestration model.** Can run as a native `Glue.StartJobRun.Sync`
  Step Functions task (same DAG style as the rest of the pipeline) or on an independent
  EventBridge monthly schedule; either way it's one more IAM-scoped job, not a parallel
  architecture.
- Once this exists, all 10 shards (of both files) can run in a single monthly job — the
  Lambda-side day-of-year shard rotation (`a4d1352`) was a workaround for Lambda's ceiling,
  not a data-coverage feature, and can be retired once Glue takes over the live fetch.

## Alternatives considered

- **AWS Fargate (ECS scheduled task).** Same "no timeout ceiling" benefit, but requires a
  container image (this codebase ships as a Lambda zip today, no Dockerfile anywhere) plus
  a cluster/task-definition/networking setup — meaningfully more moving parts than a script
  + IAM role for a job that is fundamentally "download some files, filter rows, write a
  small CSV." Rejected as more infra than the problem needs.
- **AWS Batch.** Same rejection as Fargate, plus its own layer of compute
  environment/job-queue/job-definition on top — more setup than Fargate for equivalent
  capability. Rejected.
- **Glue crawler + Athena.** Athena is built for querying data already staged in S3 in a
  columnar/structured form; it does not solve the actual bottleneck here, which is
  fetching ~6GB from an external HTTP mirror in the first place. A crawler + Athena query
  would need a separate fetch step anyway, adding cost and moving parts without removing
  the core constraint. Rejected — this was the ADR-011-era assumption ("a future
  Athena/Glue pre-staged partition"); the Glue *Python Shell* option was not considered
  at the time and turns out to subsume it directly.
- **Bump Lambda memory/timeout further.** Not available — 15 minutes is a hard AWS-wide
  Lambda ceiling, already hit in live testing at max ephemeral storage.

## Plan

1. Independent of this ADR (pure code, no infra): join `Empresas{n}.zip` (razão social)
   for Estabelecimentos rows with a blank trade name, currently silently dropped into
   `no_name` and never proposed. Tracked separately — see the linked issue.
2. A thin Glue-job entrypoint that calls the already-shipped `receita_bulk.fetch_shard`
   / `propose_candidates` for all 10 shards of both files, sequentially, once per run (no
   rotation needed once the ceiling is gone).
3. CDK: one Glue Python Shell Job resource + IAM role (read/write on the entities +
   curation-log tables; the mirror is public HTTP, no egress config needed) + a monthly
   EventBridge schedule.
4. Retire the Lambda-side live-shard-fetch default path (`a4d1352`'s workaround) once the
   Glue job is live and verified. Keep the `ONCA_RECEITA_BULK_KEY` pre-staged-partition
   read path in `lambda_port` as-is — the Glue job can write straight to that same S3 key,
   so the existing Lambda consumer needs no change.

Tracked as two issues (build here first, results feed the wider #14 discovery pipeline):
the Empresas-join gap, and the Glue execution environment (steps 2-4 above).

## Consequences & risks

- **+** #104 becomes genuinely unblockable without adopting a heavier compute platform
  (Fargate/Batch) the project doesn't otherwise need.
- **+** Cost impact is negligible (~$2/month) against the ~$100/mo budget.
- **−** Introduces Glue as a first AWS service class beyond Lambda/Step
  Functions/DynamoDB/S3/Cognito/CloudFront — a small increase in the project's
  operational surface, mitigated by Glue Python Shell's minimal setup (no cluster/VPC to
  maintain) and its narrow, single-purpose use here.
- **Risk:** the third-party mirror is the only currently Lambda/Glue-reachable host for
  this data (the official gov.br path is SSO-gated). Acceptable only because the pipeline
  is propose-only end to end (ADR 011 §4) — a stale or wrong mirror can at worst miss or
  misname a review-queue candidate, never corrupt the registry. If the mirror disappears,
  this ADR's job has nothing to fetch until a replacement source is found; that failure
  mode is a source-availability problem, not an architecture one, and doesn't change the
  execution-environment decision above.
