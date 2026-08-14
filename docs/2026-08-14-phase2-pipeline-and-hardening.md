# Phase 2 completion: live synthesis, orchestration, ingest hardening — 2026-08-14

This session took Onça from "Stage B scaffolded" to a **live, orchestrated,
hardened daily pipeline**. Landed as commits `1cea862`, `ec7a09b`, `304560c`.

## 1. Stage B synthesis went live

Flipped `OncaSynthesisLambda` from heuristic-only to real LLM synthesis after
verifying the account has access:

- `ONCA_SYNTH_USE_LLM=true`, `ONCA_SYNTH_USE_KB=true` (was `false`/`false`).
- Enabled by the Titan V2 embed quota (60 RPM, approved 2026-08-10) which
  unblocked KB ingestion; `nova-lite` Converse + KB `CQ5LBZBQTY` Retrieve
  verified live 2026-08-13.
- Output: LLM-written, source-cited fused narratives →
  `s3://onca-digests-668449743071/narratives/{date}/{id}.json`.

Two defects found while validating the live path (not by flipping the flag blindly):

- **Fake citations.** The model emitted link-mimics for source-less signals —
  `(URL: CVM-Ofertas)`, `(URL: None)`, `<BCB-Autorizacoes>`. The guardrail
  ignored them (not `http`), so they'd have shipped. Fixed by generalizing
  `scrub_fake_url_tokens` in `src/synth/citations.py` (strips `(URL: …)` and
  angle-bracket pseudo-links, keeps real `<https://…>`) plus a tightened prompt
  in `src/synth/synthesize.py`. Real URLs live in the structured `citations[]`
  array; prose refers to sources by name.
- **Nonsensical threat figures.** The feed headlined *"Itaú faces losses of
  104.51%"* — traced to real digest data: shell funds with `pl = −R$34k` yield
  meaningless `pct_change`. The R$100M AUM floor only applied to non-`is_new`
  funds; now applied on **absolute PL regardless of `is_new`**
  (`src/synth/candidates.py`).

## 2. Step Functions orchestration

Before: two independent daily EventBridge schedules (ingest, synth) with no
ordering — synth just read "latest" digest, racing ingest.

After (`infra/app.py`): one `OncaPipeline` state machine, `IngestTask → SynthTask`
sequential, triggered by a single daily rule. Sequential execution guarantees
synth reads the digest this run's ingest just wrote (synth loads the newest
object in `lambda-digests/`). Each task gets an **empty payload** so synth never
mistakes the ingest Lambda's `{statusCode, body}` return for a digest. Both tasks
retry. The two standalone rules were removed.

## 3. Ingest reliability — root cause + fix

**Symptom:** the pipeline's `IngestTask` hit `Sandbox.Timedout` at the 15-min
Lambda ceiling; it had completed within 5 min days earlier.

**First hypothesis (wrong, but hardening kept):** a slow HTTP source hanging.
Added a per-source wall-clock guard and cooperative deadline in
`src/ingest/lambda_port.py` — `_source_budget` (SIGALRM, main thread) +
`_ingest_deadline` (derived from the Lambda's remaining time). Good defense, but
it wasn't the cause.

**Actual root cause (from CloudWatch):** the handler got *past* all sources.
`DynamoDbState` stored the entire seen-set as **one** DynamoDB item; `cvm_fundos`
(24,510 funds) outgrew DynamoDB's **400 KB** item limit, so `save()` threw →
`_new_since_last_run` returned **every fund as "new"** →
`_populate_corpus_and_sync` wrote thousands of S3 objects one-by-one → timeout.
It regressed now because the state item only recently crossed 400 KB.

**Fix (`src/diff/engine.py`):**
- Shard the seen-set across `__seen__#N` items (`SHARD_SIZE=1000`) with a
  `shard_count` meta record; legacy single-item `__meta__.seen` auto-migrates on
  the next save. Verified live: `cvm_fundos` is now **25 shards**; a second run
  reported `new=0`.
- `detect_new` no longer discards the computed `fresh` set when persistence
  fails — the direct trigger of the corpus-write flood.
- Defense-in-depth cap `ONCA_MAX_CORPUS_DOCS` (default 300) in
  `src/ingest/lambda_port.py`.
- Ingest Lambda bumped to 15 min / 1024 MB; state-machine timeout 45 min.

**Result:** ingest completes in ~45–57 s; full pipeline succeeds in ~90 s.

## Verification

- `python -m pytest -q` → **84 passing**.
- Pipeline execution `green-*` → `ExecutionSucceeded`, synth wrote 9 cited
  narratives; object confirmed at `narratives/2026-08-12/cand-ent-itau.json`.

## Toolchain notes (environment gotchas)

- `npx`/`cdk`/`npm` on this repo (`/mnt/d`) resolve to **Windows** binaries, but
  `node` is Linux. Drive the CDK CLI with Linux node against the Windows install:
  `/usr/bin/node "/mnt/c/Users/MY/AppData/Roaming/npm/node_modules/aws-cdk/bin/cdk" <cmd>`.
- `build/lambda` is a **hand-staged** asset (git-ignored). Before each deploy:
  `rsync -a --delete --exclude=__pycache__ src/ build/lambda/src/`. A real build
  step is future work.
- Pushing to GitHub requires the user's credentials; this environment can't push.
