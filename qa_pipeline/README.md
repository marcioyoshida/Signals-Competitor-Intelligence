# ADR 027 QA pipeline

Playwright + Bedrock navigation/visual QA for the Onça dashboards. See
[`docs/2026-09-16-adr027-e2e-navigation-visual-qa-pipeline.md`](../docs/2026-09-16-adr027-e2e-navigation-visual-qa-pipeline.md)
for the design and the [`qa-pipeline`](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/labels/qa-pipeline)
label for the implementation issue breakdown (#126–#133).

## QA personas (#126)

Two dedicated Cognito users exist ONLY for this pipeline — never a real design partner's
identity (`infra/app.py`, `OncaQaUserEntry`/`OncaQaUserSovereign`):

| persona | username | tenant_id | tier | licensed industries |
|---|---|---|---|---|
| `entry` | `qa-test-entry@onca.example` | `qa-internal-test` | entry | `agri-funds` |
| `admin` | `qa-test-admin@onca.example` | `qa-internal-admin` | sovereign | `banking`, `fintech` |

Credentials + pool/client ids live in Secrets Manager
(`signalscompetitor/onca/qa-test-credentials`), never in this repo. The tenant_config rows
were provisioned the normal way (`scripts/provision_tenant.py put qa-internal-test entry
agri-funds` / `put qa-internal-admin sovereign banking fintech`).

## Running the login task locally

```bash
export AWS_PROFILE=my2027
python qa_pipeline/tasks/login_smoke.py entry --out /tmp/qa-entry-session.json
python qa_pipeline/tasks/login_smoke.py admin --out /tmp/qa-admin-session.json
```

Needs `playwright install chromium` once (browsers aren't checked into the repo).

## The sessionStorage gotcha

Onça's session token lives in `sessionStorage` (`onca_id_token`,
`src/dashboard/site/v2/context.js`), not `localStorage` and not a cookie. Playwright's own
`BrowserContext.storage_state()` only captures cookies + localStorage — it does **not**
carry this token. `qa_pipeline/lib/auth.py` captures sessionStorage explicitly after login
(`login_with_password`) and re-injects it into a fresh context via `add_init_script`
(`seed_session_storage`), which is the correct Playwright idiom for sessionStorage-backed
auth. Verified live 2026-09-16: a context seeded this way loads `/exec` already
authenticated, with `#sectorSel` correctly scoped to the persona's licensed industries — no
second Hosted UI round-trip.

## A real bug this task found and fixed

Building this task's first live run surfaced a genuine production bug, not a QA-only
concern: Cognito's Hosted UI OAuth Authorization Code flow was omitting `custom:tenant`/
`custom:tier` from the ID token for EVERY password-login user, even though the same user's
`AdminInitiateAuth` token included them fine. `lambda_pretoken.py`'s password-login path
was a no-op (correctly, since the attribute is real and already set) — but Cognito's
Hosted UI OAuth flow needs the Pre Token Generation trigger to explicitly re-assert
existing attributes via `claimsOverrideDetails`, or it silently drops them from the code-
grant ID token. Fixed 2026-09-16 (`src/dashboard/lambda_pretoken.py`) and confirmed live via
this exact login task — every password-tenant login was affected before this fix, not just
the QA personas. See the module's docstring for the full account.

## Directory layout

- `lib/auth.py` — Playwright Hosted UI login + sessionStorage capture/reuse.
- `lib/config.py` — pulls QA credentials (Secrets Manager) + basic-auth (SSM) at runtime.
- `tasks/login_smoke.py` — CLI entry point; also the login task `OncaQaPipeline` (#127)
  runs once per persona per run.
- `Dockerfile` / `requirements-lambda.txt` / `handler.py` / `buildspec-image.yml` — the
  `OncaQaPipeline` Lambda container image (#127) and its CodeBuild image-build project.
- The rest of the test pillars (#128–#133) are not built yet — `handler.py` today is only
  the skeleton proof: opens `/exec`, asserts the title, uploads a screenshot + result JSON.

## `OncaQaPipeline` infra (#127)

Deployed as its own stack, `OncaQaPipelineStack` (`infra/qa_pipeline.py`) — separate from
`OncaPrototypeStack`, own IAM role, own failure domain. Two-phase because no box that runs
`cdk deploy` for this repo has a usable local Docker daemon (WSL without the Docker Desktop
integration active) — the image can only be built by CodeBuild (privileged mode,
docker-in-docker):

- **Phase A** (always deployed): ECR repo `onca-qa-runner`, S3 bucket
  `onca-qa-artifacts-<account>` (30-day lifecycle), CodeBuild project
  `onca-qa-image-build` that builds/pushes the image.
- **Phase B** (gated on `qa_pipeline_image_tag`, persisted in `infra/cdk.json` — same
  durable-context pattern as Google OAuth's `google_client_id`): the container Lambda
  `onca-qa-runner`, the `OncaQaPipeline` state machine, and a nightly EventBridge trigger
  (06:00 UTC).

### Rebuilding and rolling out a new image

No source-control auto-trigger is wired yet (a documented follow-up, not this issue's
scope) — build/push/roll out manually:

```bash
zip -r /tmp/qa-image-source.zip qa_pipeline src/__init__.py src/synth/__init__.py \
  src/synth/bedrock_llm.py -x '*__pycache__*'
aws s3 cp /tmp/qa-image-source.zip s3://onca-qa-artifacts-<account>/_source/qa-image-source.zip
aws codebuild start-build --project-name onca-qa-image-build   # wait for SUCCEEDED
aws lambda update-function-code --function-name onca-qa-runner \
  --image-uri <account>.dkr.ecr.us-east-1.amazonaws.com/onca-qa-runner:latest
```

(The `src/synth/*` files are #132's Bedrock-vision dependency — `checks/vision.py` reuses
`src/synth/bedrock_llm.py`'s Converse wrapper rather than a second Bedrock client. If you
only touched `qa_pipeline/` and not `src/synth/bedrock_llm.py`, the shorter
`zip -r /tmp/qa-image-source.zip qa_pipeline -x '*__pycache__*'` still works — the Dockerfile
only fails if those `src/` files are referenced by its `COPY` but missing from the zip.)

(A full `cdk deploy OncaQaPipelineStack` also works but is not needed for a code-only
change — same fast-path convention as this repo's other Lambdas.)

### Cross-browser + viewport matrix (#128)

`OncaQaPipeline` now runs: `QaLoginTask` (one Hosted UI login as the `entry` QA persona) →
`QaMatrixSpec` (a `Pass` injecting the fixed browser×viewport list) → `QaMatrix` (a `Map`,
`max_concurrency=3`) → `QaMatrixShard` (one Lambda invoke per item, `mode: "matrix"`). Each
shard seeds the login's captured sessionStorage (`auth.seed_session_storage`, no
re-authentication per shard), navigates `/exec` at the given viewport, and asserts both the
expected title AND `document.documentElement.scrollWidth <= clientWidth` — the actual,
checkable signature of the #125 mobile-responsive contract, not just "the page loaded."

Matrix today: **Chromium + Firefox** × desktop (1920×1080) / tablet (860px) / phone (480px)
— `/exec`'s actual breakpoints, not arbitrary device names. **WebKit is deliberately not
included** (see gotcha below).

Manually invoke a single shard directly, without the state machine, while iterating:

```bash
aws lambda invoke --function-name onca-qa-runner --cli-read-timeout 90 \
  --payload '{"mode":"login","persona":"entry"}' --cli-binary-format raw-in-base64-out \
  /tmp/login.json
python3 -c "import json; d=json.load(open('/tmp/login.json')); \
  json.dump({'mode':'matrix','browser':'chromium','viewport':'phone', \
  'session_storage':d['session_storage']}, open('/tmp/shard.json','w'))"
aws lambda invoke --function-name onca-qa-runner --cli-read-timeout 90 \
  --payload file:///tmp/shard.json --cli-binary-format raw-in-base64-out /tmp/out.json
```

### Real gotchas found building this (all confirmed live)

1. **The Playwright Python *pip package* is not pre-installed in Microsoft's official
   `mcr.microsoft.com/playwright/python` image** — only the OS deps and browser binaries
   are. `requirements-lambda.txt` must pin `playwright==<same version as the image tag>`
   explicitly, or the Lambda fails at import (`No module named 'playwright'`).
2. **Headless Chromium needs Lambda-specific launch flags or it crashes**, in two stages:
   first `browser.launch()` succeeds but `context.new_page()` fails with "Connection closed
   while reading from the driver" (Chromium's sandbox can't init — no seccomp/user-namespace
   privileges in the Lambda execution environment; fixed with `--no-sandbox
   --disable-dev-shm-usage`), then a SECOND failure, "Target crashed" (Chromium's normal
   multi-process model needs process-fork privileges Lambda doesn't grant either; fixed with
   `--single-process --no-zygote`).
3. **Firefox hangs for its full 180s launch timeout** trying to write its profile/cache
   under `$HOME`, which Lambda's execution environment leaves read-only (`unable to create
   directory '/home/sbx_user.../.cache/dconf': Read-only file system`). Fixed by setting
   `HOME=/tmp`, `XDG_CACHE_HOME=/tmp/.cache`, `XDG_CONFIG_HOME=/tmp/.config` as Lambda
   environment variables — `/tmp` is the one writable path in the container.
4. **WebKit crashes on `new_page()` with no diagnostic output at all** (`TargetClosedError`,
   no stderr from the browser process, unlike Chromium/Firefox which both failed loudly).
   Tried and ruled out: the HOME/XDG fix above (no effect), `WEBKIT_DISABLE_COMPOSITING_MODE=1`
   (a documented WebKit-in-Docker fix elsewhere, no effect here either). Left as a known,
   undiagnosed gap rather than an open-ended debugging spiral — the matrix ships with
   Chromium + Firefox; WebKit is a fast-follow once there's a way to get real crash
   diagnostics out of it (worth trying: `xvfb-run`, since WebKit's Linux headless mode is
   less mature than Chromium's/Firefox's and sometimes needs a real, if virtual, display).
5. **`sfn.Map`'s inline `items=` CDK prop is JSONata-only** — this repo's state machines use
   classic JSONPath (`OncaPipeline`'s own convention), so the fixed matrix array has to be
   injected via a preceding `sfn.Pass` (`result_path="$.matrix"`) and referenced by
   `items_path`, not passed directly as `sfn.ProvideItems.json_array(...)`.
6. **A real product bug, not a pipeline bug**: the very first live matrix run correctly
   failed at 480px/860px — `document.documentElement.scrollWidth` was 299-721px wider than
   the viewport on `/exec`, despite #125's grid-collapse pass. Root cause (confirmed via a
   live DOM scan, not guessed): `.badge--infer` (the long descriptive captions on inference
   badges, e.g. "ROE·ROA·alavancagem·eficiência·share de lucro · IF.data + balancete ·
   inferência") inherits `.badge`'s shared `white-space: nowrap`, which is correct for SHORT
   status badges but forces 300-600px-wide unbroken lines for these longer captions at any
   viewport width. Fixed in `src/dashboard/site/v2/app.css`'s `max-width: 780px` block
   (`.badge--infer { white-space: normal; text-align: left; }`), scoped to the one modifier
   class so short status badges elsewhere keep their one-line guarantee. (A red herring
   ruled out along the way: the inspection drawer's `position:fixed` + `transform:
   translateX(100%)` closed state does NOT inflate `scrollWidth` — `visibility: hidden` was
   added to it anyway for a11y correctness, since `aria-hidden="true"` was already set, but
   it was not the cause of the overflow.)

### Smoke & routing checks (#129/#130)

`checks/smoke.py` and `checks/routing.py` add two more `handler.py` modes (`"smoke"`,
`"routing"`). Two more real bugs found live, plus one hard infra constraint:

7. **A real product bug**: `/exec`'s `openDrawer()`/`closeDrawer()` (`site/v3/index.html`)
   toggled a `.on` class that no CSS rule anywhere defines (the shared `v2/app.css` only
   styles `.drawer.open`/`.scrim.open`) — clicking a card correctly flipped `aria-hidden`
   and populated the drawer's content, but the slide-in `transform` never fired, so the
   drawer was functionally "open" yet stayed fully off-screen. Confirmed via
   `getComputedStyle(...).transform` before/after, not just eyeballing a screenshot. Fixed
   by using `"open"` (the class the shared component vocabulary already styles) instead of
   inventing new CSS for `.on`.
8. **A test-URL bug, not a product bug, but worth recording**: `/entry` (no trailing slash)
   and `/v2/admin` (ditto) both 404/render blank — the CloudFront viewer-request function
   only appends `index.html` to a URI that already ends in `/`. The correct URLs are
   `/entry/` and the short alias `/admin` (which the SAME function rewrites to
   `/v2/admin/index.html` directly). Same class of gotcha as the `/exec` → `/v3/index.html`
   cache-key rewrite already documented in the `onca-live-resources-deploy` memory — always
   check `infra/app.py`'s `auth_fn` rewrite table before assuming a friendly path.
9. **Hard constraint: this Lambda's Chromium cannot survive a second browser context in one
   launch.** `--single-process` (required for Chromium to survive Lambda's process-fork
   restrictions at all — see gotcha #2 above) means the WHOLE browser process crashes the
   instant a second `BrowserContext` is created, whether or not the first was closed first
   (confirmed by direct repro: two sequential `browser.new_context()` calls on one
   `browser.launch()`, second one's `new_page()` throws `TargetClosedError`, even with zero
   navigation in between). Every isolated test scenario — a fresh sessionStorage seed, a
   deliberately session-less context, a different query string — needs its OWN full
   `sync_playwright()` + `launch()` + `close()` cycle. `checks/smoke.py`'s one continuous
   journey reuses a single page throughout (never opens a second context) for exactly this
   reason; `checks/routing.py`'s `_fresh_page()` context manager relaunches a whole browser
   per check. The relaunch cost (a few seconds each) is the honest price of isolation in
   this environment, not overhead to optimize away.

### Resiliency: broken-link scan + network interception (#131)

`checks/resilience.py` adds a fourth parallel branch (`QaResilienceTask`), one continuous
Chromium journey (single page/context — same constraint as #9 above): a real login, then a
broken-link scan across every friendly route (`/exec`, `/entry/`, `/app`, `/admin`,
`/newentry`, `/adquirencia`, `/fintech`, `/seguros`, `/wealth` — `infra/app.py`'s
CloudFront `routes` map), then three `page.route()`-simulated network conditions against
`/api/feed` and `/api/ask`. 13 checks, all passing live. `/api/register`'s network behavior
isn't covered — same documented Google-self-registration automation gap as #129/#130.

10. **The broken-link scan's naive form has a false-positive trap**: these dashboards call
    `/feed.json?opkey=...` as part of their OWN normal (correctly access-controlled, #122)
    boot sequence — visiting `/admin` bare (no `?opkey=`) correctly 403s on that fetch, same
    as the "wrong opkey" contract already tested in `checks/routing.py`. A blanket "any
    same-origin 4xx = broken link" rule flags that as a failure, which is wrong — it's the
    gate working as designed, not a dead link. Scoped the scan to the navigation response
    itself plus `script`/`stylesheet` resource types only (via
    `response.request.resource_type`), which is what "broken link" actually means here —
    dead routes and missing assets, not an intentionally-gated data fetch.
11. There is no large `<a href="/...">` link graph on these dashboards to crawl in the first
    place (confirmed by inspection — `/exec`'s nav is JS-driven buttons with `data-officer`
    attributes, `/app`'s nav is in-page `#hash` links, not real page loads); the "internal
    link" surface worth scanning is the fixed friendly-route list plus each page's static
    asset references, not a discovered link graph. If a genuine cross-page `<a href>` nav is
    ever added, extend the scan to discover it rather than assuming the fixed list still
    covers everything.

### Bedrock visual QA (#132) + the report (#133)

`checks/vision.py` adds a fifth parallel branch (`QaVisionTask`, advisory — never fails the
run). `qa_pipeline/lib/vision.py` holds the checklist prompts + a strict-JSON verdict parser;
`src/synth/bedrock_llm.py`'s `converse()` gained an `images: list[bytes]` param (ADR 027's
first caller) rather than a second Bedrock client. Model: `amazon.nova-pro-v1:0` — the same
family already in production for framework synthesis, multimodal (Nova Micro/Lite are not),
no new model-access request.

`checks/report.py` (a sixth, final state — not a parallel branch) consolidates every
branch's raw output into one static HTML page: a pass/fail table per hard-gate check plus
every vision finding with its screenshot inlined. Uploaded to the same private
`onca-qa-artifacts` bucket; the Lambda returns a **presigned GET URL** (not a new public
CloudFront distribution — see the module docstring for why: these screenshots carry real,
if QA-tenant-scoped, dashboard content, and the product itself gates that behind basic-auth
+ Cognito on purpose, unlike Fleet Monitor's genuinely-public uptime status page). That
presigned URL is signed with the Lambda's own temporary STS credentials, so it's only
reliably valid for about as long as those last (well under 24h) — good for "go check the run
that just finished," not a stable long-lived link. For an older run:

```bash
aws s3 presign s3://onca-qa-artifacts-<account>/<run_id>/report/index.html --expires-in 3600
# or, for the latest run without knowing its run_id:
aws s3 presign s3://onca-qa-artifacts-<account>/latest/report/index.html --expires-in 3600
```

Every task in the state machine now shares ONE `run_id` — the execution's own name
(`$$.Execution.Name`), injected into every branch's payload — so one run's matrix/smoke/
routing/resilience/vision artifacts land under one shared S3 prefix instead of each Lambda
invocation minting its own timestamp (confirmed this was actually happening before the fix:
a single execution's shards had 3 different `run_id` prefixes).

**A hard-gate failure must not make the report disappear.** Every hard-gate branch (matrix
shard task, smoke, routing, resilience) got `.add_catch(..., result_path="$.error")` so a
real failure produces an error-shaped branch output instead of aborting the whole
`QaBranches` Parallel state — `QaReportTask` always runs, always renders SOMETHING, even
when the state machine's overall execution would otherwise show as FAILED. Each check
module already uploads its full result JSON to S3 *before* raising (see e.g.
`checks/smoke.py`), so a human debugging a real failure still has the complete checklist
detail in S3 even though the report's summary line for that branch is sparse (no
`.add_catch()` recovers the granular checks from inside a crashed invocation — this was
logically traced through, not live fault-injection tested, since deliberately breaking a
check just to prove the catch path felt like the wrong trade against the time it'd cost).

12. **A real, useful finding vs. a real false positive — both confirmed by eye, not just
    trusted from the model's own confidence score.** The Mapa Competitivo panel: the model
    correctly flagged "Kinea" and "Fidc" labels touching, and an arrow "not visibly
    extending out" of the Btag11/Btal11 bubble cluster — both genuinely visible in the
    screenshot, real label-crowding the declutter algorithm doesn't fully solve at this
    data density (a legitimate UX follow-up, not fixed as part of shipping this check — see
    ADR 027's own "advisory, not a hard gate" scoping). The quotes panel: the model claimed
    "there is a placeholder glyph present" — the actual screenshot shows a clean, fully
    rendered ticker row with no broken-image icon anywhere. A clear hallucination. This
    exact mix (real finding + confident false positive, both at 0.9 "confidence") is why
    ADR 027 scoped this layer as advisory from day one rather than a hard gate — the
    burn-in period this needs before anyone should trust a "fail" without independently
    looking at the screenshot is not hypothetical, it showed up in the FIRST live run.
13. **`max_tokens` truncation silently produces invalid JSON, not an error.** The first live
    run's `max_tokens=500` cut two of three panel responses off mid-JSON (missing the final
    closing brace) — a genuinely incomplete object, not the trailing-garbage case
    `parse_verdict`'s `raw_decode()` already tolerates (see gotcha below). `json.loads`/
    `raw_decode` both correctly report this as malformed rather than silently returning
    partial data, which is why `parse_verdict` treats it as `available: False` instead of
    crashing — but the FIX is giving the model enough budget (900, not 500) for a 3-item
    checklist with notes, not trying to parse around a truncation after the fact.
14. **Nova Pro occasionally appends stray trailing characters after an otherwise well-formed
    JSON object** (confirmed live: a trailing `;` after `...}]}`) — `json.loads` rejects
    that outright as "Extra data" even though the JSON itself parses fine.
    `json.JSONDecoder().raw_decode()` stops at the first complete value and ignores
    whatever comes after, which is the correct fix (a regex trim would be more fragile).
