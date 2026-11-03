# ADR 030: Monitoring the signed-in path, and budgets below the hard limits

- Status: accepted, live in Onça on 2026-11-03.
- Scope: Onça first. Written as the fleet standard, so other forks can adopt it as it is.
- Related: #109 (pipeline and feed-staleness alarms), ADR 016 (multi-tenant), ADR 027 (E2E QA),
  and Fleet Monitor.

## Context: two incidents in one evening

1. **Google login returned HTTP 500 on `/exec`** (2026-11-03, 00:30Z).
   - The Cognito Pre Token Generation trigger imported boto3 inside a 128 MB invocation. That
     took about 7.5 s twice.
   - Cognito allows a trigger 5 s and then retries. `/oauth2/token` outlasted the session
     broker's 10 s read timeout, and the unhandled `ReadTimeout` became a 500.
   - Password logins never reach that branch, so only Google users saw it.
2. **"Feed indisponível (HTTP 500)"** (seen 01:40Z; the API 5xx started at 20:00Z on 11-02).
   - After a pipeline run, `feed.json` reached 7.2 MB. The widest tenant's scoped `/api/feed`
     response passed Lambda's 6 MB response cap: 413 `RequestEntityTooLarge`, then
     `Runtime.ExitError`.
   - It stayed broken for about 5 hours before the owner reported it.

Neither raised an alarm. What existed:
- pipeline failed or timed out;
- the feed heartbeat (`FeedPublished`);
- storefront upgrade errors.

Fleet Monitor probes anonymous endpoints, so it cannot see what a signed-in browser sees. Both
failures were **hard platform limits** reached gradually: a time budget (Cognito's 5 s) and a
size budget (Lambda's 6 MB). Nothing measured the distance to either limit.

## Decision

Six layers. Each is cheap, and each one alone would have caught at least one of the two
incidents.

### 1. Error alarms on every user-path function, plus the API's own 5xx

- One metric-math alarm per path sums `Errors` (and `Throttles` where concurrency matters)
  across that path's functions:
  - `Onca-SignInErrors`: pre-token, session, OAuth, self-register;
  - `Onca-UserApiErrors`: feed, Ask, Agentic API, act, push, MCP, Ibis hook.
- `Onca-Api5xx` is on the HTTP API's `5xx` metric. **Lambda's `Errors` does not count a
  handled 500**: a handler that catches, logs and returns 500 is a success to Lambda.
- `Onca-Cdn5xx` fires at 5 or more 5xx responses in 5 minutes at the edge. The alarm uses
  requests × error rate, so one failed request out of two at 3 a.m. doesn't page.

### 2. Budgets: alarm on the distance to a hard limit, not on the limit

| Limit | Hard value | Alarm / check at |
|---|---|---|
| Cognito trigger time | 5 s, then retry | `Onca-PreTokenSlow`: max > 3 s/h; canary: billed < 3 s |
| Lambda response payload | 6,291,556 B | `Onca-FeedResponseSize` > 4 MB; canary < 4.5 MB |
| API Gateway integration | 30 s | session broker 28 s; its Cognito call (5 + 20 s) |
| CloudFormation resources/stack | 500 | canary check < 485 |
| SES bounce rate | 10%: sending paused | `Onca-SesBounce` > 5% |
| ACM renewal | expiry | `Onca-CertExpiry` < 30 days (renewal stalled) |
| Step Functions state payload | 256 KB | covered by #173 S3 hand-off; listed for forks |

Every fork keeps a table like this in its own monitoring doc. **Any new integration with a
platform limit adds a row before it ships.**

### 3. Size and growth watch on generated artifacts

- The feed builder emits `Onca/FeedJsonBytes` and `FeedGrowthPct` against the previous
  publish.
- `Onca-FeedGrowth` fires when `|growth| > 30%` in one run:
  - a jump risks the cap;
  - a drop usually means a source returned nothing.
- The feed API emits `FeedResponseBytes` (the payload actually returned) through an
  embedded-metric log line: no API call, no IAM.
- Fix for the cap itself: `/api/feed` is gzip-compressed (base64, `content-encoding`), always
  when the body is over 1 MB. The widest tenant fell from over 6 MB to 1.3 MB.

### 4. An authenticated synthetic canary, every 4 hours

`OncaCanary` (`src/dashboard/canary.py`) runs on EventBridge `rate(4 hours)` and costs no
Bedrock call.

**No passwords.** It invokes the feed Lambda directly with the JWT claims the authorizer would
forward (`sub`, `custom:tenant`). It invokes the pre-token trigger with a synthetic Google
event on a `.invalid` address, which can never map to a tenant.

Checks:
- **feed as a signed-in tenant:** the widest tenant (`owner-all-sectors`) and the outside
  reviewer tenant;
- **pre-token timing**, cold or warm;
- **public routes:** page 200, `/api/feed` 401 unauthenticated, each tenant domain;
- **session broker to Cognito:** a dummy code must come back 401 within 8 s;
- **stack resource budget.**

Outputs:
- metrics `Onca/Canary` `CanaryRun` and `ChecksFailed`;
- the report at `s3://<digests>/ops/canary/latest.json`, which Fleet Monitor reads.

**One alarm for both failure modes:** `IF(runs < 1, 1, failed)` over 6 h, with missing data
as breaching. A failed check and a canary that stopped running page alike.

### 5. Guard tests that keep the limits from coming back

`tests/test_monitoring.py` checks:
- every user-path Lambda sits in an error alarm;
- each math alarm stays within CloudWatch's 10-metric limit;
- the canary is 4-hourly and its alarm covers a missed run;
- sign-in Lambdas have at least 512 MB;
- the canary budgets sit below the hard limits;
- both incidents, replayed, fail the canary.

A network-marked test scopes **today's real `feed.json`** for an all-modules tenant through
the real handler and checks the payload budget.

### 6. Alerts that say what to do

- Each alarm carries a description: what broke, the limit, where to look.
- The notifier adds it to the Teams, Slack or email message, and OK (recovery) is sent too.
- Alarm names are explicit (`Onca-…`) instead of CloudFormation-generated.

### 7. Prove delivery, not just wiring

The first delivery test found that **no alarm had ever reached anyone**. The notifier returned
`teams/slack/email = None` because `ONCA_ALERT_EMAIL_FROM` and `ONCA_ALERT_EMAIL_TO` were set
neither in the environment nor in `/onca/api-key`. Every pipeline-failure and stale-feed alarm
since #109 was dropped without a trace. `alertEmail` sat unused in `cdk.json`.

- The notifier now sends from `alertas@onssa.org` (SES-verified domain, production access) to
  the stack's `alertEmail`.
- A test fails if the channel is ever unset again.
- **Rule for every fork:** after wiring alarms, invoke the notifier with a test message and
  check that its delivery report shows a real send (an SES message id or a webhook 2xx).
  `AlarmActions` being set proves nothing.

## Fleet adoption checklist

1. List the signed-in user's path end to end, including identity-provider triggers. Put one
   math-alarm group on it, plus the API's 5xx metric.
2. Fill in the budget table (layer 2) for your stack. Add a check or alarm at about 60–80% of
   each limit.
3. Emit size metrics for every generated artifact that a request returns whole.
4. Add a canary that invokes your handlers with synthetic claims (no password, no account)
   every 4 h. It writes `ops/canary/latest.json` for Fleet Monitor.
5. Add guard tests for memory floors, timeouts and alarm membership.
6. Route alarms through your existing notifier with descriptions; send OK as well.
7. Send one test alert and confirm it arrives (layer 7). Repeat after every change to the
   notifier or its configuration.

## Costs

- 11 alarms (several multi-metric) plus the canary: about $1–3 per month.
- 15 stack resources. Onça went from 462 to about 477 of 500.

## Not built yet (candidates, by value)

- **Stack split.** Onça is about 23 resources from CloudFormation's hard limit. The next large
  feature needs a nested or sibling stack first. The canary's stack check is the warning.
- **Browser-side errors** (JavaScript exceptions, failed fetches in `/exec`): CloudWatch RUM or
  a tiny `/api/client-error` beacon. This is the only layer that sees what a user sees, so it
  would have caught the feed outage within minutes.
- **Spend anomaly:** AWS Cost Anomaly Detection on Bedrock, Lambda and S3 requests. Free; cf.
  Tarantula's S3-request bill.
- **Per-source ingestion yield:** a source returning zero items for N runs (the "gnews fallback
  masks dead feeds" trap).
- **Bedrock throttling** on Ask (`InvocationThrottles`) and DynamoDB throttles: rare on
  on-demand, cheap to add to the user-API group once it has room.
