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

    src/ingest/    BCB normativos, IF.data market share, CVM fund filings
    src/diff/      change detection (the product's atomic unit)
    run.py         digest runner → data/latest_digest.json
    config/        watchlist (competitors, lookback)
    infra/         AWS bootstrap for account 668449743071
