# Signals — Onça competitive intelligence

Agentic AI competitive intelligence for Brazilian financial services:
regulatory changes (BCB/CVM/SUSEP) + competitor signals + market data,
fused into one threat-scored feed with source citations on every claim.

Target distribution: AWS Marketplace. Stack: AWS-native serverless
(Lambda, S3, Bedrock Knowledge Base + S3 Vectors, AgentCore).
AWS account: my2027 (668449743071), region us-east-1.

- **Full project context and decisions:** [CLAUDE.md](CLAUDE.md)
- **Setup and first run:** [GETTING_STARTED.md](GETTING_STARTED.md)

## Quick start

    pip install -r requirements.txt
    python run.py

First run seeds state; subsequent runs report genuine deltas only.
See CLAUDE.md "caveats" — API response field names need a one-time
alignment against live responses.

## Layout

    src/ingest/    BCB normativos, IF.data, Pix DICT keys, autorizações,
                   CVM funds, SEC filings, Lambda handler
    src/diff/      change detection (detect_new + detect_moves)
    run.py         local digest runner → data/latest_digest.json
    config/        watchlist (competitors, ISPB, thresholds)
    infra/         CDK stack (Lambda, DynamoDB, S3, Bedrock KB)
    docs/          CONTEXT, DATA_SOURCES (live schemas), phase notes

## Status (2026-07-19)

Phase 1.5 Lambda digests: normativos, CVM funds, IF.data, autorizações,
Pix DICT keys, and BCB juros médios (daily pricing). Phase 2 Stage A KB
infra is up; embedding sync still blocked on Bedrock quota. Details:
[docs/CONTEXT.md](docs/CONTEXT.md), [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).
