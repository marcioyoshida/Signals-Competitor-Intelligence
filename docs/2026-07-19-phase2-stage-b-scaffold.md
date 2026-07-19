# Phase 2 Stage B scaffold — synthesis / correlation — 2026-07-19

## Goal

Turn the daily multi-source digest into **flagged narratives with source
citations** — the product differentiator. Stage B does **not** wait on
Bedrock KB embeddings: it ships a **digest-first** path that always works,
and an **optional Retrieve** path when Stage A ingestion is unblocked.

## Architecture (decided)

| Piece | Choice | Why |
|---|---|---|
| Runtime | Plain Lambda | Once-daily batch; no session state (AgentCore Runtime deferred) |
| Input | Latest S3 digest (`onca-digests-*/lambda-digests/`) | Already produced by ingest Lambda |
| Corpus | Optional `bedrock-agent-runtime Retrieve` | Enrich when KB is live |
| Cheap model | Nova Micro / Haiku (when Converse allowed) | Route/classify candidates |
| Strong model | Sonnet / Nova Pro (when allowed) | Write narrative |
| Offline / test | Heuristic synthesizer | No Bedrock required; enforces citation rules |
| Guardrail | Code, not prompt-only | Drop any URL not in the allowed source set |
| Output | S3 `narratives/{date}/{id}.json` | Feeds Phase 3 dashboard/alerts |

## Pipeline

```
latest digest (S3)
    → candidates.extract_candidates(digest)
    → [optional] retrieve.enrich(candidate) from KB
    → synthesize.narrative(candidate, sources)
    → citations.enforce(narrative, allowed_urls)
    → write S3 + return summary payload
```

### Candidate extraction (v1 heuristics)

From one digest payload:

1. **Regulatory** — each `new_normativos` / `regulatory.items` item is a
   seed candidate (threat_hint: regulatory).
2. **Competitor fusion** — attach same-window competitor signals
   (funds, ofertas, entrants, pix_moves, juros_moves) as related context
   when name/CNPJ substrings overlap, or as separate competitor candidates
   if no regulatory seed exists.
3. Cap volume (`ONCA_SYNTH_MAX_CANDIDATES`, default 10) for cost control.

### Citation guardrail (`src/synth/citations.py`)

- Collect allowed URLs from source records (`url` fields).
- Find `http(s)://…` in narrative text.
- If a URL is **not** allowed → remove the sentence containing it.
- Attach `citations: [{url, source_id?}]` only from the allowed set used.
- Narratives with **zero** remaining citations are dropped (no uncited
  claims in the product feed).

### Bedrock degradation

| Call | On failure |
|---|---|
| Retrieve | Skip enrichment; use digest sources only |
| Converse (router/synth) | Fall back to heuristic narrative |
| No model access | Heuristic only |

## Code map

| Path | Role |
|---|---|
| `src/synth/citations.py` | Guardrail |
| `src/synth/candidates.py` | Digest → candidates |
| `src/synth/digest_io.py` | Load latest digest from S3 / body |
| `src/synth/retrieve.py` | Optional KB Retrieve |
| `src/synth/bedrock_llm.py` | Converse wrapper |
| `src/synth/synthesize.py` | Heuristic + LLM narrative builders |
| `src/synth/lambda_handler.py` | Stage B entrypoint |

## CDK

- Second Lambda: `OncaSynthesisLambda`, handler
  `src.synth.lambda_handler.lambda_handler`, same `build/lambda` asset.
- Env: digests bucket, raw bucket, KB id, model ids, max candidates.
- IAM: `s3:GetObject` digests, `s3:PutObject` narratives prefix,
  `bedrock:Retrieve`, `bedrock:InvokeModel` / `Converse`.
- Schedule: daily EventBridge (offset best-effort until Step Functions).

## Not in this scaffold

- Step Functions orchestration (ingest → wait → synth)
- SNS/email delivery (Phase 3)
- Threat scoring model (placeholder `threat_score` heuristic only)
- True AgentCore Runtime

## Hardening (2026-07-19, no Bedrock)

- Ingest digests now carry **`context` samples** (not only delta `items`)
  and tag deltas with `is_new`, so Stage B still fuses after seeding.
- Entity alias map (`src/synth/entities.py`) fuses SEC tickers, CVM leaders,
  admins, and market labels (NU/Stone/Itaú/BTG/…).
- Candidate kinds: `entity_fusion`, `regulatory_fusion`, `competitor:*`.
- Heuristic narratives list multi-lens evidence with citation URLs.

## Verification

```bash
python -m pytest tests/test_synth_*.py -q
# Local dry run with a digest fixture:
python -m src.synth.lambda_handler
# Against a saved digest JSON:
python -m src.synth.lambda_handler /tmp/real-digest.json
# Against latest S3 digest:
ONCA_DIGESTS_BUCKET=onca-digests-668449743071 python -m src.synth.lambda_handler --s3
# After deploy:
aws lambda invoke --function-name <OncaSynthesis...> --payload '{}' out.json
```
