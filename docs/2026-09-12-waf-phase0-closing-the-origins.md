# WAF Phase 0 — closing the origins (Onça)

**Status:** implemented, not yet deployed. **Date:** 2026-09-12.
**Scope:** `Signals-Competitor-Intelligence` (Onça) only. The other seven Signals
stacks carry the same defect and are tracked separately.

## Why this comes before any web ACL

The portfolio audit found one AWS WAF web ACL across eight deployed stacks, and it
protects nothing an attacker would target. The reason is structural:

> **AWS WAF cannot be attached to a Lambda function URL, and it cannot be attached
> to an API Gateway HTTP API (v2) either.** Supported targets are CloudFront, ALB,
> API Gateway REST (v1), AppSync, Cognito user pools, App Runner and Verified Access.

Every public entry point in this portfolio is one of the two unsupported types. A web
ACL on the CloudFront distribution therefore only filters traffic that *chooses* to
arrive through CloudFront. Onça had five Lambda function URLs at `AuthType NONE`, each
one a publicly addressable `*.lambda-url.us-east-1.on.aws` hostname that bypassed the
edge basic-auth Function, the routing rewrites, and any future rate rule.

Adding a web ACL first would have produced a firewall in front of open doors. Phase 0
closes the doors. **This phase carries the actual security benefit; the web ACL in
Phase 1 is the cheap part.**

## What changed

### 1. Every function URL is now `AWS_IAM` behind CloudFront OAC

`infra/app.py`. One shared `FunctionUrlOriginAccessControl` (`furl_oac`,
`Signing.SIGV4_ALWAYS`) serves all four remaining origins. CDK emits an
`AWS::Lambda::Permission` per function granting `lambda:InvokeFunctionUrl` to
`cloudfront.amazonaws.com`, scoped by `SourceArn` to this distribution alone.

| Behavior | Lambda | Was | Now |
|---|---|---|---|
| `/api/*` (catch-all) | `OncaReviewAction` | NONE | AWS_IAM + OAC |
| `/api/registry/*` | `OncaRegistryApi` | NONE | *no function URL* — cut over to the JWT HTTP API on `main` |
| `/api/quotes*` | `OncaQuotesApi` | NONE | AWS_IAM + OAC |
| `/api/run/*` | `OncaRunTrigger` | NONE | AWS_IAM + OAC |
| `/api/act*` | `OncaActApi` | NONE | AWS_IAM + OAC |

`/api/ask`, `/api/gaps` and `/api/feed` were already cut over to the Cognito-JWT HTTP
API and have no function URL. `/api/registry/*` joined them on `main` (commit
`489a20f`) while this branch was open; removing the origin outright is strictly
better than signing it, so that cutover was taken as-is on merge. Its break-glass
secret leg now calls the same `origin_secret_ok` helper for the constant-time,
fail-closed compare.

`SIGV4_ALWAYS` is load-bearing, not a default worth leaving implicit. CloudFront must
**overwrite** the `Authorization` header on the origin request: the dashboard sends
`Authorization: Basic …` for the edge basic-auth Function, the origin request policy
`ALL_VIEWER_EXCEPT_HOST_HEADER` forwards it, and `SIGV4_NO_OVERRIDE` would leave that
Basic header in place and fail every signature.

### 2. The origin-secret gate now fails closed

All seven handlers used one of two forms:

```python
if secret and headers.get("x-onca-origin") != secret:   # operator endpoints
origin_ok = (not secret) or headers.get("x-onca-origin") == secret   # ask / gaps
```

Both **disable the check when `ONCA_ORIGIN_SECRET` is unset**. A dropped environment
variable published the endpoint — the failure mode you least want as the default.

Replaced by `src/dashboard/auth.py:origin_secret_ok`, which denies on an unset or empty
secret and compares with `hmac.compare_digest`. Callers: `review_action`,
`registry_api`, `run_trigger`, `act_api`, `quotes_api`, `agent_ask`, `gaps_api`.

### 3. The dashboard sends the SigV4 payload hash

**This is the part that breaks silently if it is missed.** Lambda function URLs do not
accept `UNSIGNED-PAYLOAD`, and CloudFront does not hash the request body on your
behalf. For `POST`/`PUT` the **viewer** must send the SHA-256 of the body in
`x-amz-content-sha256`; CloudFront folds that header into the signature it computes.
Without it every write returns `InvalidSignatureException`.

`oacFetch()` was added to the shared runtime (`site/v2/app.js`, exported on
`window.OncaUI`) and inline in `site/index.html`, which does not load `app.js`. Call
sites converted: 5 in `site/index.html`, 4 in `site/v3/index.html`, 1 in
`site/v2/admin/index.html`.

Two `navigator.sendBeacon` calls to `/api/act` in `v3/index.html` became
`oacFetch(..., { keepalive: true })`. `sendBeacon` cannot set request headers, so it
cannot carry the payload hash at all. `fetch` with `keepalive` keeps the
survive-unload guarantee for bodies this small.

The helper falls back to a plain `fetch` when `crypto.subtle` is unavailable (a
non-secure context, e.g. the file opened locally) — there is no CloudFront on that
path either, so there is nothing to sign for.

## Verification performed

- Full suite green: **1114 passed** (pre-merge count; see below for the post-merge run) (1110 before, plus 4 fail-closed regression tests
  and the `origin_secret_ok` unit tests in `tests/test_auth.py`).
- `cdk synth` succeeds, and the template asserts: 4/4 `AWS::Lambda::Url` at
  `AWS_IAM`; one lambda-type OAC with `SigningBehavior: always`; 4
  `lambda:InvokeFunctionUrl` permissions all scoped to the distribution ARN; 4/4
  function-URL origins carrying both the OAC and the origin-secret header.
- The `sha256Hex` logic was executed against Node's `crypto` and matches
  `createHash("sha256")` for empty, ASCII and accented pt-BR bodies.

A CDK synth test is deliberately **not** in `tests/`: `buildspec.yml` runs `pytest`
*before* staging `build/lambda`, so a synth test would fail on asset resolution in CI.

## Verification still required at deploy time

The signing path cannot be exercised without a real distribution. Do all of it — the
Bluefin roadmap recorded its WAF as "verified" on the one path that was already
protected, and missed exactly this.

1. **Origin unreachable.** For each of the four functions, read `FunctionUrl` from the
   Lambda console and `curl` it directly. Expect **403** (`AccessDeniedException`).
   This is the whole point of the phase; test it per function, not once.
2. **Edge still works.** Through the distribution: `GET /api/quotes`, `GET /api/run/`.
3. **Writes still work.** This is where an OAC rollout fails. In the browser, approve a
   review item (`POST /api/review`) and click *Executar* (`POST /api/run/`). A
   `502`/`InvalidSignatureException` in the network tab means the payload hash is not
   reaching the signer — check that `x-amz-content-sha256` is on the request and that
   the behavior still uses `ALL_VIEWER_EXCEPT_HOST_HEADER`.
4. **v3 telemetry.** Confirm the `/api/act` beacons return 2xx now that they are
   `fetch(keepalive)` rather than `sendBeacon`.
5. **Rollback.** Revert the commit and redeploy. Nothing here is stateful, and no data
   is migrated.

## Follow-on phases

| Phase | Work | Effort | Cost |
|---|---|---|---|
| 1 | One shared CloudFront-scoped web ACL in a `SignalsEdgeSecurity` stack, ARN published to SSM, read by each product stack | ½ day | $5/mo |
| 2 | Four rate-based rules tiered by endpoint class, `count` first then `block`; managed common rule set last, also in count | ½ day | ~$5/mo |
| 3 | `BlockedRequests` alarms dimensioned on the rule's **metric name** (Bluefin's alarm uses its construct name and can never fire); sampled requests on; the AWS Budget that still does not exist | ½ day | $0 |
| 4 | Per-endpoint verification at both the edge and the origin hostname | — | — |

One shared ACL across all nine distributions costs roughly **$10/month** against the
~$100 ceiling; one ACL per stack would be roughly **$56/month**. AWS WAF is $5.00 per
web ACL, $1.00 per rule, $1.00 per managed rule group and $0.60 per million requests.

Onça's own Phase 1 blocker: the other seven stacks must reach Phase 0 before a shared
ACL is worth attaching to them, or their function URLs stay bypassable.

## Sources

- [Restrict access to an AWS Lambda function URL origin](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-lambda.html)
- [CloudFront OAC for Lambda function URL origins (GA, April 2024)](https://aws.amazon.com/about-aws/whats-new/2024/04/amazon-cloudfront-oac-lambda-function-url-origins/)
- [AWS WAF pricing](https://aws.amazon.com/waf/pricing/)
