# Lambda diff wiring — 2026-07-12

## Problem

`lambda_handler` (Phase 1.5 Lambda port) re-fetched and re-published the
full BCB normativos and CVM fund lists on every invocation. The diff
engine (`src/diff/engine.py`) already implemented "what's new since last
run" for the local (`run.py`) path via `JsonState`, and `infra/app.py`
had already provisioned a DynamoDB state table and wired
`ONCA_STATE_TABLE` into the Lambda's environment — but `lambda_handler`
never called into the diff engine. Every daily run reported the same
items as "the digest," which defeats the product's stated atomic unit:
*"X is new since last run."*

## Change

`src/ingest/lambda_port.py`:

- Added `_new_since_last_run(source, docs)`, which calls
  `detect_new(source, docs, state=DynamoDbState(source))` and falls back
  to treating all docs as new if the state table is unreachable (same
  degrade-gracefully pattern already used for the ingester fetch calls).
- `lambda_handler` now diffs `bcb_normativos` and `cvm_fundos` results
  through this helper before building the payload.
- Payload gained a `new_count` field per source; `items` now holds the
  *new* docs (previously it held the first N of *all* fetched docs).
- `market` (IF.data) is deliberately left undiffed — it's a full
  quarterly ranking snapshot with no per-institution `id`, not a stream
  of discrete new/seen events, so `detect_new`'s id-set semantics don't
  apply.

## Testing

No second day of real ingest data exists yet to exercise the diff
against, so the regression test mocks two successive `fetch_recent`
results against one shared in-memory fake DynamoDB table
(`FakeStateTable` in `tests/test_lambda_port.py`), simulating "yesterday"
and "today":

- `test_lambda_handler_reports_only_new_normativos_across_two_runs` —
  day 1 fetches docs A and B (both reported new); day 2 fetches A, B,
  and C against the same fake table (only C reported new).
- `test_lambda_handler_treats_all_as_new_when_diff_state_unavailable` —
  confirms the fallback path when the state table raises.
- The 6 pre-existing tests were updated to stub `_new_since_last_run` as
  an identity function, keeping them focused on ingester/S3 failure
  behavior rather than incidentally exercising the diff path.

All 11 tests in `tests/` pass.

## Remaining gap

The diff has only been exercised against mocked data — it has not yet
been run against two real consecutive days of live BCB/CVM data. That
validation should happen once the Lambda is actually deployed and runs
on its daily EventBridge schedule.
