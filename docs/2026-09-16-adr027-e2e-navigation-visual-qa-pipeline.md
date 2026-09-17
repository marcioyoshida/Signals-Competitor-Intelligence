# ADR 027 — End-to-end navigation + AI visual QA pipeline (Playwright + Bedrock, on Lambda/Step Functions)

- Status: **Proposed** — 2026-09-16. Owner-requested.
- Builds on: the `OncaPipeline` Step Functions DAG pattern (`infra/app.py` — Parallel/Map
  tasks coordinating only through S3, empty payloads, one shared Lambda asset), the
  Cognito identity + per-tenant read boundary (ADR 002 Phase D), the industry-sector
  scoped-session fix on `/exec`/`/app` (2026-09-12, commit `aa87ade`), Google OAuth +
  lazy Entry self-registration (2026-09-15,
  [`docs/google-oauth-runbook.md`](google-oauth-runbook.md)), the mobile-responsive pass
  (#125), the coverage-gap auto-remediation loop (ADR-014), and the Nova-family Bedrock
  usage already live in `src/synth/bedrock_llm.py`. Does **not** replace or modify
  `OncaPipeline` (the data pipeline) — this is a second, independent state machine.

## Context

Onça's test suite today is ~1200 deterministic `pytest` tests, gated in CI
(`buildspec.yml`, in-account CodeBuild). That suite verifies logic, not the live,
rendered browser experience. Zero browser-level navigation or visual test exists as
checked-in, repeatable infrastructure.

In practice, browser-level verification has happened only ad hoc: during an interactive
session, a throwaway local Playwright harness gets built, the live site's JS is
extracted (regex, from the deployed HTML), screenshots are rendered headless, a human
(or the assisting model) eyeballs the result, and the harness is discarded. This
worked — it caught real production bugs mid-session that no pytest test could have
(2026-09-15/16, Mapa Competitivo panel on `/exec`): labels rendering on top of their own
bubble because the offset wasn't radius-aware, delta-whisker arrowheads fully occluded
inside opaque dot fills because the geometry didn't clear the dot's own radius, and a
CloudFront cache-key mismatch that kept serving a 19-hour-stale build while invalidation
reported "Completed." None of these are things a DOM assertion or a pixel-diff would
reliably catch either — they are *legibility* defects, visible only to something that
looks at the rendered image and judges it the way a person would.

That workflow has three structural problems: it leaves no artifact, it is not repeatable
(rebuilt from scratch every time), it does not run automatically after a deploy, and it
depends entirely on a human — or an LLM session — choosing to do it by hand. Meanwhile
several real, live surfaces now depend on navigation/auth/routing behaving correctly for
actual paying and prospective tenants, with no regression coverage beyond a one-time
manual click-through at ship time: Cognito Hosted UI login (password + "Continue with
Google" + lazy Entry self-registration), hash-based deep-linking with per-tenant
licensed-sector scoping on `/exec` and `/app`, the two-tier basic-auth/operator-secret
edge (#122), and the `/exec` mobile-responsive layout (#125).

The owner asked for a comprehensive automated navigation/E2E suite (smoke, deep-linking,
cross-browser/viewport, resiliency) plus a new layer — AI image inference against
Bedrock — that inspects rendered screenshots for the class of defect pixel-diff/DOM
assertions structurally miss, orchestrated on Lambda + Step Functions rather than a
GitHub Actions/GitLab CI runner.

## Decision

Stand up a second, independent state machine, **`OncaQaPipeline`**, separate from
`OncaPipeline`. Different trigger cadence, different blast radius, different IAM role —
it must never share a failure domain with content ingestion. Lambda tasks package
Playwright as **container images** (not zip/layers — three browser binaries exceed the
250 MB unzipped layer ceiling; container images support up to 10 GB and CDK's
`DockerImageFunction` is a native, already-idiomatic construct for this). A Step
Functions **Map** state fans the browser × viewport matrix out in parallel. A dedicated
Bedrock-vision task — extending the existing `bedrock_llm.converse()` helper to accept
image content blocks, model `amazon.nova-pro-v1:0` (same family already in production
use for framework synthesis, vision-capable, no new model-access request needed) — runs
as an **advisory, non-blocking** check on a curated set of chart-bearing screenshots,
not a hard gate. That caution is deliberate, mirroring this codebase's existing
confidence-gated patterns (ADR-006 auto-approval, the coverage-gap loop's
`AUTO_CODEGEN=off` default): an LLM's subjective visual judgment should not fail a
production deploy until its false-positive rate is known from real runs.

### 1. Core testing pillars → Onça-specific coverage

The generic pillars map onto Onça's actual surfaces as follows (there is no
checkout/cart — the "core action" completion state is a scoped feed rendering with real
data for the tenant's licensed industries):

- **Smoke & critical path.** Cognito Hosted UI login (password); "Continue with Google"
  → lazy Entry self-registration form → industry picks persisted → scoped tenant
  created (2026-09-15 feature); logout; session persistence across a hard reload
  (`sessionStorage` id token, `Ctx.isLoggedIn()`). Landing on `/exec` or `/app` → pick a
  sector → officer board renders → open the inspection drawer, as the success path.
- **Header/nav equivalents.** Officer tabs (CSO/CRO/CCO/CPO on `/exec`), the sector
  `<select>`, theme toggle, drawer close/scrim, the "Perguntar à Onça" Q&A panel,
  `/entry`'s portal nav, `/v2/admin`'s curation nav — every one must actually route/
  render, not merely exist in the DOM.
- **Deep linking.** `/exec#<sector>` and `/app#<slug>` hash routes: (a) a licensed slug
  survives a reload; (b) an unlicensed or garbage slug falls back to the first licensed
  sector rather than rendering unscoped or blank data (`activeSector()`/
  `licensedIndustries()` contract shipped 2026-09-12); (c) an unauthenticated deep link
  renders `Ctx.mountGate`'s honest gate, never a stale or empty-looking board. This
  pipeline is what keeps that contract true after every future change to `/exec`/`/app`.
- **Dynamic routing / query params.** `?admin=1&opkey=...` operator bypass must reject a
  missing or wrong `opkey` (#122 — the shared basic-auth password is explicitly NOT an
  operator credential).
- **Browser history.** Back/forward across hash changes on `/exec`/`/app` must not
  desync the `SECTOR`/`LICENSED` in-memory state from the URL (`hashchange` listener).
- **Multi-tab/popup.** Any external link (docs, ToS, once added) opens in a new
  tab/context without disturbing the original tab's state (Playwright multi-context).

### 2. Cross-browser & responsive

- Chromium, Firefox, WebKit — matrixed via a Step Functions `Map` state, one Lambda
  invocation per `{browser, viewport}` pair, `maxConcurrency` capped (cost + Bedrock
  quota discipline), not unbounded.
- Viewports: desktop 1920×1080, plus the two breakpoints `/exec`'s mobile-responsive
  pass (#125) actually targets — `max-width: 860px` and `max-width: 480px`
  (`src/dashboard/site/v3/index.html`) — verifying the stacked-grid/touch-target
  behavior #125 shipped, not an arbitrary device name.

### 3. Resiliency & error handling

- **Broken-link scan** — crawl every internal href/route reachable from `/exec`, `/app`,
  `/entry` once per run. A 404/500 fails the run outright. This is the one **hard gate**
  in the whole pipeline besides `pytest` itself — a dead link is unambiguous, not a
  judgment call.
- **Network interception** (`page.route()`) — slow-3G, offline, and API-timeout
  simulation against `/api/feed`, `/api/ask`, `/api/register`; assert a graceful
  loading/error state renders, never a blank board or an uncaught console exception.

### 4. Bedrock image-inference visual QA (the new layer)

What it catches that the above cannot: label/mark occlusion, illegible contrast, "does
this delta arrow visually point where the underlying data says it should" — precisely
the three defect classes found by hand this week on the Mapa Competitivo panel.

- **Flow.** A Playwright screenshot task uploads a PNG to
  `onca-qa-artifacts/<run>/<test>.png` → a Lambda task calls the extended
  `bedrock_llm.converse()` with the image plus a structured, per-panel-type checklist
  prompt (e.g., for a bubble/scatter chart: "are any two labels overlapping," "is any
  arrow/whisker fully hidden inside a mark," "does any text fail contrast against its
  background") → the model returns a structured JSON verdict per checklist item
  (`pass`, `confidence`, `note`).
- **Advisory, not blocking, at launch.** A fail or low-confidence verdict is attached to
  the HTML report and can feed the existing coverage-gap auto-issue machinery
  (ADR-014) rather than failing the pipeline. Promote to a hard gate only after a
  burn-in period establishes a real false-positive rate — the same caution ADR-006 and
  ADR-014 already apply to their own auto-decisions.
- **Cost containment.** Run the vision check against a small, curated set of
  chart-bearing panels per run (Mapa Competitivo, the threat×expansion quadrant map,
  the quotes panel), not every screenshot in the full browser/viewport matrix —
  most matrix screenshots are already covered structurally by pillars 1–3; multiplying
  every viewport × every browser × a Bedrock call would buy little marginal signal for
  real marginal cost.

## Pipeline infrastructure

- **Execution.** `OncaQaPipeline` — its own state machine, its own IAM role, no
  ingestion-pipeline coupling. Lambda tasks as container images (ECR); ephemeral storage
  raised (up to 10 GB `/tmp`) for trace/video capture before S3 upload. Each Map-shard
  Lambda stays under Lambda's 15-minute ceiling; the pasted spec's "5–10 min total
  runtime" target is met through the Map state's parallelism, not one long-running
  function.
- **Trigger.** EventBridge nightly schedule (primary). A manual `StartExecution` hook is
  also wired from `OncaCicdStack`'s `buildspec.yml` as a **fire-and-forget post-deploy**
  smoke check — it must not block `cdk deploy`'s own CodeBuild phase on a 5–10 minute
  browser suite, which would roughly double every deploy's wall time for a check about
  the *live* site, not the deploy artifact.
- **State management.** A login task runs once per run per test persona (a dedicated
  `qa-internal-test` Entry-tier tenant plus an internal/admin persona, provisioned via
  `provision_tenant.py --email`/`--google-email` against a tenant reserved for this and
  never a real design partner's), writes Playwright's `storageState` JSON to S3; every
  downstream Map-shard Lambda reads it instead of re-authenticating — same idea as
  local-file `storageState` reuse, S3-backed because Lambda invocations share no
  filesystem.
- **Artifacts.** HTML reporter output plus screenshots/video/trace, uploaded to a new,
  short-lived (30-day expiration) `onca-qa-artifacts` bucket — debug output, not a data
  asset. A lightweight run index (reuse the Fleet Monitor static-status-page pattern
  rather than inventing a new one) so a human opens the latest run without downloading
  anything.
- **Flakiness mitigation.** Web-first assertions and auto-waiting locators only — zero
  `page.waitForTimeout()`. Given Lambda cold starts, the first navigation per shard gets
  one Step Functions-native `Retry` on the task (not a Playwright-level sleep loop), to
  absorb cold-start latency without masking a real app bug as "flaky."

## Consequences

- New, separate spend: Lambda container-image compute (nightly, matrix-parallel,
  concurrency-capped), Bedrock Nova Pro vision calls (bounded to curated panels), ECR
  image storage, a small S3 bucket. Size this against the pipeline-engineer's ~$100/mo
  constraint as its own line item, not folded into `OncaPipeline`'s budget; default to
  nightly-only (not per-PR) until real cost data exists.
- A dedicated QA test tenant/personas must exist and be clearly named so QA traffic is
  never mistaken for a real design partner's usage in `onca-tenant-config` or telemetry.
- Bedrock-vision verdicts are advisory only at launch — a deliberate scope limit.
  Whether/when to promote to a hard gate is a follow-up decision, not part of this ADR.
- This does not replace the ad hoc local-Playwright-harness pattern used mid-session for
  one-off design verification (e.g. this week's Mapa Competitivo work) — that stays the
  right tool for interactive debugging *during* a change. `OncaQaPipeline` is what runs
  unattended *after* a change ships, continuously.

## Alternatives considered

- **GitHub Actions / GitLab CI as the executor**, as the pasted spec assumed — rejected
  as the primary executor. This repo already standardized on in-account CodeBuild (no
  GitHub-hosted runner — see `buildspec.yml`'s own header comment) and on a serverless,
  no-long-lived-server operating model (`OncaPipeline`). Lambda + Step Functions was the
  explicit ask and also fits the codebase's own grain — it reuses the
  Parallel/Map-task + S3-mediated-coordination shape already in `infra/app.py` rather
  than introducing a second orchestration paradigm. CodeBuild is kept only as the
  deploy-time *trigger*, never the test executor.
- **Pixel-diff visual regression** instead of/alongside Bedrock vision — rejected as the
  *sole* visual check. A pixel diff flags any change, including legitimate data churn,
  which is constant here since every panel renders live data, not a static mock — too
  noisy to be useful. Bedrock vision judges semantic correctness ("is anything
  occluded/illegible"), which tolerates normal data movement. Not mutually exclusive: a
  future increment could add pixel-diff for the genuinely static chrome (nav, header)
  where a stable baseline is meaningful.
- **A Claude-family Bedrock model** for the vision check — no functional blocker, but
  Nova Pro is already the in-production model family for this account
  (`src/synth/bedrock_llm.py`), so reusing it avoids a new model-access request and
  keeps one Bedrock relationship instead of two.

## Open questions / follow-ups

Not blocking this ADR's acceptance, to resolve before or during implementation:

- Whether the "core action" success path needs one flow per tenant tier (Entry portal /
  SaaS `/app` / Sovereign) rather than a single shared flow — the tiers render
  meaningfully different data shapes.
- Whether Bedrock-vision findings should feed the existing coverage-gap GitHub-issue
  auto-open machinery (ADR-014) directly or use a dedicated label — leaning toward
  reusing ADR-014 rather than building a parallel mechanism, flagging because ADR-014
  wasn't designed with visual findings in mind.
- GitHub issue breakdown, filed 2026-09-16 (label `qa-pipeline`):
  [#126](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/126)
  Phase 0 QA tenant/persona provisioning,
  [#127](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/127)
  Phase 1 `OncaQaPipeline` scaffold,
  [#128](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/128)
  Phase 2 cross-browser/viewport matrix,
  [#129](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/129)
  Phase 3 smoke & critical-path nav,
  [#130](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/130)
  Phase 4 deep-linking/routing/history,
  [#131](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/131)
  Phase 5 resiliency (broken-link hard gate + network interception),
  [#132](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/132)
  Phase 6 Bedrock image-inference visual QA,
  [#133](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/133)
  Phase 7 artifacts/reporting index. Order: #126/#127 unblock everything else;
  #128 (matrix) should land before #129–#131 so those tests run cross-browser
  from day one rather than being retrofitted onto it later.
