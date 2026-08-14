# Phase 3 plan: warroom dashboard — 2026-08-14

Next step chosen at the end of the 2026-08-14 session. **Not yet started** — two
decisions are still open (see bottom). This doc is the starting point for the
next session.

## Goal

Surface the daily cited narratives (currently only in S3) as a warroom UI:
threat-scored feed, KPI tiles, entity timeline, and per-narrative source
drill-down. This is the first thing that makes the product demoable to design
partners.

## Data available

~10–14 narratives/day, ~10 days of history under
`s3://onca-digests-668449743071/narratives/{date}/{id}.json`.

Per-narrative schema:
`id, kind, entity, entities[], lenses[], is_alert, threat_score,
threat_score_note, narrative, citations[] ({url} | {id, source}),
source_ids[], as_of, data_as_of{}, mode`.

Note: `threat_score` is still a **placeholder heuristic** (`threat_score_note:
estimated_heuristic`). Real threat scoring is separate future work; the dashboard
should show the score but not imply it is a validated model.

## Architecture (fits CLAUDE.md: AWS-native, CFN/Marketplace, ~$100/mo, no idle floor)

- **Static site: S3 + CloudFront (OAC).** No idle floor, CFN-deployable for
  Marketplace Quick Launch. No API Gateway / dynamic backend — the data updates
  once a day.
- **Feed builder Lambda** (`src/dashboard/feed_builder.py`): scan `narratives/`
  for the last N days, aggregate into one `dashboard/feed.json` (feed items +
  per-entity timelines + KPI rollups), write to the site bucket. Wire as a third
  pipeline step after `SynthTask` in `OncaPipeline` (or fold into synth).
- **Frontend** reads the tiny static `feed.json`. Follow the **dataviz** skill;
  run `scripts/validate_palette.js` before shipping.

## v1 scope

- Threat-scored feed: sorted by `threat_score`, filter by entity / lens / alert.
- KPI tiles: today's narrative count, alerts, entities tracked, distinct sources.
- Entity timeline: score over the ~10-day window (per entity).
- Narrative drill-down: full text + `citations` (linked) + `source_ids`.
- Light/dark, accessible (legend + table view per dataviz non-negotiables).

## Files (next session)

- `infra/app.py` — site bucket + CloudFront (OAC) + feed-builder Lambda + pipeline
  step; optional auth (see below).
- `src/dashboard/feed_builder.py` (+ handler), `tests/test_feed_builder.py`.
- Frontend asset dir (deployed as a CDK/S3 asset).

Remember `build/lambda` is hand-staged: `rsync src/ build/lambda/src/` before
deploy, and drive CDK via Linux `node` (see the 2026-08-14 hardening doc).

## Open decisions (resolve first thing next session)

1. **Frontend build** — buildless single-file (vanilla JS + inline SVG;
   recommended, avoids the Windows/WSL npm friction) **vs** React SPA + build step.
2. **Auth** — basic-auth via a CloudFront Function (recommended for a prototype)
   **vs** open private URL **vs** Cognito hosted UI. The feed is competitive
   intelligence, so open-by-URL is prototype-only.

## Verification (when built)

`cdk deploy`, run the pipeline, open the CloudFront URL, confirm the feed renders
from `feed.json` with working filters, timeline, and citation links.
