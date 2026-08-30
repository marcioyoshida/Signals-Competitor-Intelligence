# OncaPipeline — topology, contracts & constraints (living doc)

- **Owner:** the `pipeline-engineer` subagent. This is the source-of-truth map for the
  orchestration layer; keep it reconciled with `infra/app.py` on every structural change.
- Seeded 2026-08-30 from `infra/app.py`. State machine `OncaPipeline` (live ARN in the
  `onca-live-resources-deploy` memory), account `my2027` / us-east-1.
- Shape: two-phase parallelized DAG (issue #10). Tasks pass **empty payloads** and
  coordinate **only through S3 + DynamoDB** — the ordering constraints below are *data*
  dependencies, not control flow.

## DAG

```mermaid
flowchart TB
  subgraph ING["Ingest (Parallel) — same Lambda, mode payload"]
    S["structured  {mode:structured}"]
    N["news  {mode:news}"]
  end
  ING --> FE[feature] --> SY[synth]
  SY --> BA
  subgraph BA["BeliefAxes (Parallel) — belief feeders, MUST precede SWOT"]
    silence
    longitudinal
    comparative
    thematic
    cohort
  end
  BA --> DET
  subgraph DET["Detectors (Parallel)"]
    direction TB
    subgraph SW["swot_branch (sequential)"]
      swot --> reconcile --> seed --> maintenance --> FW
      subgraph FW["Frameworks (Parallel)"]
        tows
        porter
        pestle
        ansoff
        bcg
        four_corners
        seven_s
      end
      FW --> autoapprove
    end
    regulatory
    threads
    behavioral
    relational
    subgraph QSA["qsa_branch (sequential)"]
      qsa --> operatives
    end
    predictive
    ecosystem
  end
  DET --> feed
```

Chain: `Ingest → feature → synth → BeliefAxes → Detectors → feed`. Critical path ≈ the
`swot → reconcile → seed → maintenance → Frameworks → autoapprove` branch (not the sum
of ~24 steps). State-machine timeout **45 min**.

## Ordering invariants (breaking these silently corrupts the run)
1. **Ingest before feature/synth.** `structured` writes the base digest
   `lambda-digests/<id>.json` (+ owns the raw corpus); `news` writes
   `lambda-digests/news/<id>.json`; synth (`digest_io.load_latest_digest_from_s3`)
   overlays the latest news slice onto the latest base.
2. **BeliefAxes before SWOT.** SwotTask builds beliefs from *this run's* axis narratives
   carrying a `swot_hint` (comparative/cohort/thematic) or deriving S/W
   (longitudinal/silence) — so all five belief feeders must finish before the SWOT branch
   or the hints only land next run (via the 90-day window).
3. **SWOT store is sequential:** build → reconcile → seed → maintenance. Frameworks are
   **parametric over the SWOT store** (read it), then autoapprove folds their proposals in.
4. **feed runs LAST** — `feed_builder` reads `narratives/{date}/…` + registry rollups. A
   step whose output must reach the dashboard has to run **before** `feed`.
5. The Detectors NOT feeding/read-by SWOT (regulatory, threads, behavioral, relational,
   qsa→operatives, predictive, ecosystem) run concurrently with the SWOT+frameworks branch.

## S3 / state contracts
- **Digests bucket** `onca-digests-668449743071`: `lambda-digests/<id>.json` (base),
  `lambda-digests/news/<id>.json` (news slice), `narratives/{date}/cand-*.json`,
  `features/latest.json`, `coverage_gaps/index.json`, `distress/index.json`,
  swot/framework stores.
- **Site bucket** (…`oncadashboardsite`…): `feed.json` (written by feed-builder).
- **DynamoDB**: registry `…OncaEntitiesTable…` (ENT#/ALIAS#/CNPJ#), state/seen-set
  `…OncaStateTable…` (sharded `__seen__#N`; **Decimal, never float**).

## Per-Lambda config (memory / timeout) — pipeline tasks
| Task (SM) | Lambda | mem MB | timeout |
|---|---|---|---|
| Ingest (structured ∥ news) | OncaLambdaPrototype (`ingest.lambda_port`) | 1024 | 15 min |
| synth | OncaSynthesisLambda | **1536** | 5 min |
| feature | OncaFeatureStore | 512 | 5 min |
| silence / longitudinal / comparative / thematic / cohort | Onca{…} | 512 | 5 min |
| regulatory / threads / behavioral / relational / operatives / predictive / ecosystem | Onca{…} | 512 | 5 min |
| swot / swot_reconcile / swot_seed | Onca{…} | 512 | 5 min |
| swot_maintenance | OncaSwotMaintenance | 256 | 2 min |
| tows/porter/pestle/ansoff/bcg/four_corners/seven_s | Onca{…} | 256 | 3 min |
| autoapprove | OncaAutoApprove | 256 | 2 min |
| watchlist_qsa | OncaWatchlistQsa (`ingest.watchlist_qsa`) | 256 | 5 min |
| feed | OncaFeedBuilder | 512 | 5 min |

**Off-chain Lambdas** (API/UI, not in the state machine): OncaRunTrigger (starts the SM),
OncaAgent (`/api/ask/`, 512/60s), OncaGapsApi (512/90s), OncaRegistryApi, OncaReviewAction
(256/30s). Changes to these don't affect DAG timing.

## Constraints to keep
- **Lambda CPU scales with memory** (512MB ≈ 0.36 vCPU). CPU-bound steps (synth: candidate
  extraction / resolve_entities over the growing registry) need memory headroom — synth is
  at 1536MB for this reason. Right-size here, not by raising timeouts.
- **Per-source budget** `ONCA_SOURCE_TIMEOUT_SEC=90` (SIGALRM) bounds each ingest source so
  a slow endpoint can't eat the 15-min ingest; a source hitting it is skipped, not fatal.
- **Bedrock** model quotas/throttling (synth + framework drafters call Converse); healthy
  ≈ 1–3 s/call. Watch for throttle storms when many framework branches fire concurrently.
- **Cost ceiling ~$100/mo, no idle floor** (S3 Vectors KB, not OpenSearch). New always-on
  resources or big memory bumps must respect it.
- **Tuning knobs** (env, in `infra/app.py`): `ONCA_SYNTH_MAX_CANDIDATES`, `ONCA_SYNTH_MIN_*`,
  `ONCA_SOURCE_TIMEOUT_SEC`, `ONCA_*_MAX_ENTITIES`, `ONCA_RESOLVE_PREFILTER`,
  `ONCA_ENTITY_DISCOVERY*`, `ONCA_FIAGRO_*`. A CLI-set env/memory is wiped by the next
  `cdk deploy` — make durable changes here.

## Deploy (DIRECT — not the onca-cicd pipeline)
- DAG/infra/Lambda-config changes: `cd infra && cdk deploy OncaPrototypeStack`.
- Code-only: fast-zip `aws lambda update-function-code` per changed function (rsync
  `src/`→`build/lambda` first). See the `onca-live-resources-deploy` memory.
- Trigger a run: `aws stepfunctions start-execution --state-machine-arn <arn> --input '{}'`
  (`AWS_PROFILE=my2027`); watch `describe-execution` / `get-execution-history`.
