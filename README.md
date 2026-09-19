# Signals — Onça competitive intelligence

Agentic AI competitive intelligence for Brazilian financial services:
regulatory changes (BCB/CVM/SUSEP/PREVIC/SPA) + competitor signals + market
data, fused into one threat-scored feed with source citations on every claim.

Stack: AWS-native serverless (Lambda, Step Functions, DynamoDB, S3, Bedrock
Knowledge Base + S3 Vectors, Cognito, CloudFront).
AWS account: my2027 (668449743071), region us-east-1.

- **Full project context and decisions:** [CLAUDE.md](CLAUDE.md)
- **Setup and first run:** [GETTING_STARTED.md](GETTING_STARTED.md)
- **Every source, its pipeline, and its effective on/off gate:** [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)
- **Architecture decisions:** the dated `docs/*-adr-*.md` series

## What the product is

Not an ingestion script — a deployed multi-tenant SaaS, live on `onssa.org`.

- **Officer dashboards** (`/exec`, ADR-021) — four role-scoped boards (CSO, CRO,
  CCO, CPO) over one shared feed, with decision capture, trajectories, and
  playbooks. Per-industry boards live under `/v2/*` (`/fintech`, `/seguros`,
  `/adquirencia`, `/wealth`) and curation under `/v2/admin`.
- **Tenancy** — Cognito (incl. Google federation) with a per-tenant read
  boundary enforced at feed-build, fail-closed. The **Entry portal** (`/entry/`,
  ADR-016) is a strict projection of the feed down to entry-tier industries.
- **Agent APIs** — grounded read-only Q&A (`/api/ask`), the write-capable
  officer agent (`/api/act`, ADR-020), and the metered **agentic API**
  (`/api/v1/agent/*`, ADR-025) with its own API-key and usage-billing rails.
- **Entity registry** — a curated commercial asset (`/api/registry`) with
  ADR-018 per-field provenance, write-precedence, an append-only mutation
  journal, and rollback. The DynamoDB table is the source of truth; code
  fixtures only seed it.

## Pipelines

Four, deployed independently:

| Pipeline | What it does | Cadence |
|---|---|---|
| `OncaPipeline` (`infra/app.py`) | ingest → features → synth → belief axes → detectors → feed | 3×/day |
| `OncaFinancialsPipeline` | prudential soundness + balancete + FinBERT-PT-BR tone (ADR-022) | monthly |
| `OncaGdeltBridgeStack` | GDELT macro-theme sweep via the AWS→GCP workload-identity bridge | daily |
| `OncaQaPipelineStack` | corpus/output QA | on demand |

Ingest is one Lambda; the Step Functions payload (`{"mode": "structured"｜"news"}`)
picks the branch. `docs/DATA_SOURCES.md` is the maintained inventory of which
pipeline invokes which module and whether it is actually on.

## Running it

**The deployed pipeline** is the real thing. To trigger a run:

    aws stepfunctions start-execution \
      --state-machine-arn arn:aws:states:us-east-1:668449743071:stateMachine:OncaPipeline64AA66FE-tGEFHlEqVym4 \
      --input '{}'

**To deploy.** New infra (tables, Lambdas, SFN tasks, IAM) needs CDK:

    cd infra && cdk deploy OncaPrototypeStack --require-approval never

Code-only changes can skip CDK via the fast direct-zip path (rsync `src/` into
`build/lambda/`, zip the **whole** directory so the vendored deps ship with it,
then `aws lambda update-function-code`). Site-only changes are an `s3 cp` plus a
CloudFront invalidation — note `/exec` is rewritten to `/v3/index.html` before
the cache lookup, so that is the path you must invalidate.

**`run.py` is a local smoke-runner, not the pipeline.** It exercises eight of the
ingest modules (BCB normativos/juros/Pix/autorizações, CVM fundos/ofertas/informe
diário, SEC filings) straight to `data/latest_digest.json` — no synthesis, no
feed, no dashboard. Useful for checking a source is still reachable; it is not a
mirror of what runs in AWS and should not be read as one.

    pip install -r requirements.txt
    python run.py

First run seeds state; subsequent runs report genuine deltas only.

## Layout

    src/ingest/     source adapters (BCB, CVM, SUSEP, PREVIC, SPA, DOU, CEIS/CNEP,
                    CADE, PNCP, DataJud, ANS, trade press, GDELT, financials…)
                    + `lambda_port.py` (the ingest handler) and `registry.py`
                    (ADR-019 declarative source/lens registry)
    src/synth/      synthesis + analysis: narratives, belief axes, detectors,
                    strategy frameworks, entity registry, executive/officer flow
    src/dashboard/  feed builder, the HTTP APIs (ask/act/agent/registry/keys),
                    and the static sites under `site/` (v2, v3, entry)
    src/diff/       change detection (detect_new + detect_moves)
    infra/          CDK: app.py (main stack), gdelt_bridge.py, qa_pipeline.py, cicd.py
    scripts/        operator tools (tenant provisioning, curation admin, pilots)
    config/         watchlist (competitors, ISPB, thresholds)
    docs/           CONTEXT, DATA_SOURCES (live schemas + coverage), the ADR series
    tests/          130 files; run with `pytest -q`

## Status

Deliberately not date-stamped here — a status block in the README rots faster
than anything else in the repo (this one sat two months and several product
generations stale, see #142). The maintained sources of truth are
[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) for coverage, the ADR series for
decisions, and the GitHub issues for open work.
