# Google OAuth runbook

Ported from Signals-Creator-Radar (Bluefin)'s `docs/google-oauth-runbook.md`, adapted for
Onça's closed tenant model. Read this before touching anything Cognito/OAuth-related — the
gotchas below were found live in the Bluefin fork, not by tests, and are structurally
identical in this codebase (same Cognito user pool shape: `custom:tenant` immutable,
`self_sign_up_enabled=False`).

## The one real difference from Bluefin

Bluefin is a self-serve consumer product — a first Google login auto-provisions a Free
tenant. **Onça mostly does not do this** — with one deliberate, narrow exception added
2026-09-15. Most tenants are operator-provisioned; a Google login for an email the operator
has already mapped via `tenant_config.map_federated_email`
(`scripts/provision_tenant.py --google-email`) resolves to that tenant's real tier
(entry/saas/sovereign) and modules. An unmapped email is not rejected at login — the trigger
just issues a token with no `custom:tenant` claim, and the existing per-tenant read boundary
(`feed_api.py`) 403s on that, rendering a gate instead of the dashboard.

**The exception**: that gate, for a federated (Google) login specifically, offers lazy
Entry-tier self-registration — `POST /api/register` (`src/dashboard/self_register.py`,
`tenant_config.self_register_entry_tenant`). The person picks which entry-tier industries
they want (agri-funds/betting/consorcio/crypto/real-estate-funds — the same
`ENTRY_INDUSTRIES` allow-list ADR 016's Entry Portal already enforces), and the endpoint
creates a brand-new `entry`-tier tenant scoped to EXACTLY that pick, then maps their email to
it. This is still not Bluefin's model: it can never grant anything above the entry tier
(server-side allow-list, not client-trusted), the tenant_id is always freshly allocated
(never caller-supplied, so it can't attach to or overwrite an existing tenant), and a
password-login user gets no such option (`self_sign_up_enabled=False` still holds — only a
Google login can even reach the authenticated-but-unprovisioned state this depends on).

## What's live (once wired)

- **Cognito domain**: `onca-<account-id>.auth.us-east-1.amazoncognito.com` (`OncaHostedUi`) —
  already provisioned unconditionally, regardless of whether Google is wired.
- **Google Identity Provider** on `OncaUserPool`, gated on `-c google_client_id=...`
  (`infra/cdk.json`, not sensitive). The client's callback/logout URLs were already literal
  strings (`onssa.org`, `www.onssa.org`, plus the bare `*.cloudfront.net` domain) before Google
  was added — never `distribution.distribution_domain_name` referenced as a construct — so
  adding the federated provider does not risk the CFN circular dependency Bluefin's runbook
  documents. Onça's login already goes through Cognito's own Hosted UI
  (`{domain}/login?...`, see `src/dashboard/site/v2/context.js`'s `login()`), so once Google is
  a `supported_identity_providers` entry on the client, Hosted UI shows a "Continue with
  Google" button automatically — **no frontend code change is needed** for the button itself.
- **Client secret**: `signalscompetitor/onca/google-oauth` in Secrets Manager
  (`{"client_id", "client_secret"}`, same shape as the existing `signalscompetitor/onca/api-key`
  secret). Never in `cdk.json`, never in git.
- **OAuth scopes requested from Google**: `openid email profile`. Onça does NOT need
  `aws.cognito.signin.user.admin` — unlike Bluefin, nothing in the dashboard calls
  `GetUserAttributeVerificationCode`/`VerifyUserAttribute` with a user's own access token, so
  there's no scope-restricted-OAuth-token gotcha to inherit here (see gotcha #3 below for what
  to check if that ever changes).
- **Attribute mapping**: `email` + `email_verified` (not `given_name` — nothing in this
  dashboard displays a user's name today; add it later if that changes, mapped the same way).
- **`src/dashboard/lambda_pretoken.py`** (Cognito Pre Token Generation trigger): injects
  `custom:tenant`/`custom:tier` via `claimsOverrideDetails` for a federated login whose email
  has a mapping row; a fast no-op for password logins (which already carry a real attribute).
- **`OncaFederatedTenantMapTable`** (DynamoDB, `email` → `{tenant_id, tier}`): populated by the
  operator via `tenant_config.map_federated_email` / `provision_tenant.py --google-email`,
  never by the trigger itself (no auto-provisioning — see above).

## The gotchas, ported from Bluefin (all avoided in the initial wiring, not just documented)

### 1. `dist.distribution_domain_name` in an OAuth callback URL = circular dependency

Not currently a live risk here — the client's callback/logout URLs already use literal
`onssa.org`/`www.onssa.org` strings (added 2026-09-13, before Google was ever considered) plus
`distribution.distribution_domain_name` for the bare CloudFront URL. Adding
`UserPoolIdentityProviderGoogle` does not introduce a NEW reference to the distribution — the
IDP construct itself takes no callback URL at all (that lives only on the client, unchanged).
Confirmed by a real `cdk synth` with `-c google_client_id=...` set: no circular-dependency
error, and the generated template shows `OncaWarroomClient` correctly `DependsOn` the IDP
resource. If a future change ever needs a NEW literal-domain reference for an IDP-specific
purpose, keep it a plain string from context/config, never a construct attribute — that's the
one thing that broke Bluefin's first attempt.

### 2. `custom:tenant` cannot EVER be written after a user is created, immutable or not

Confirmed live in the Bluefin fork via CloudWatch on its own trigger: `AdminUpdateUserAttributes`
throws `InvalidParameterException: Attribute cannot be updated` even when the attribute was
**never set at all** on that user. Immutable means "settable only at `AdminCreateUser` time" —
and Cognito creates a federated user's record internally during the OAuth handshake, with no
attribute list Onça ever controls. There is no API call, ever, that can put a value there after
the fact.

**Do not try to fix a federated-login bug by calling `AdminUpdateUserAttributes` on
`custom:tenant`.** It will synth, deploy, and fail silently in production — an unhandled
exception in the Pre Token Generation trigger fails the entire `/oauth2/token` exchange with no
error surfaced to the browser, which looks exactly like "I got to Google's screen but it didn't
auth'd." `lambda_pretoken.py` never attempts this — it only ever reads
`OncaFederatedTenantMapTable` and injects a claims override, the same pattern Bluefin moved to
after hitting this live.

### 3. OAuth-flow access tokens are scope-restricted; native-flow tokens aren't

Not exercised by anything in this dashboard today (Onça's client scopes are just
`openid email`, no Cognito self-service API is called with a user's own access token). If a
future feature adds one (e.g. a self-service email-change flow), check whether it needs
`aws.cognito.signin.user.admin` added to **both** `infra/app.py`'s client `OAuthScope` list and
wherever the frontend builds its authorize-URL scope string — Bluefin found live that an
OAuth-flow token is scope-restricted to exactly what's requested at `/oauth2/authorize`, unlike
a native `InitiateAuth` password-login token, which isn't gated the same way.

### 4. `-c google_client_id=...` MUST be persisted in `infra/cdk.json`, not passed ad hoc

Confirmed live 2026-09-15, the hard way: the first deploy that wired Google passed
`google_client_id` only as a one-off `-c` CLI flag, never committed anywhere. ~40 minutes
later, an unrelated `cdk deploy` from a different session (no special knowledge of Google
needed — any ordinary deploy of this stack does it) ran WITHOUT that flag. Because
`google_idp` is a plain `if self.node.try_get_context("google_client_id"):` gate, that
deploy's synthesized template simply didn't have the Google branch — and CloudFormation,
seeing a template that no longer asks for `OncaGoogleIdp`/`OncaPreTokenGenFn`/the trigger/the
client's `Google` entry in `SupportedIdentityProviders`, correctly DELETED all of them. Login
kept working for password users throughout; "Continue with Google" silently vanished with no
error anywhere, for however long until the flag was passed again.

**The fix, already applied**: `google_client_id` is a real value (a Google OAuth client ID,
not a secret — the secret half lives only in Secrets Manager) committed in
`infra/cdk.json`'s `context` block. Every `cdk deploy`/`cdk diff`/`cdk synth` against this
stack — from ANY session, with or without an explicit `-c` flag — now picks it up
automatically, so an ordinary deploy of an unrelated change can no longer silently un-wire
Google login. If this client ID is ever rotated, update it in `infra/cdk.json`, not just on
the command line, or this will happen again.

### Two-node circular dependency: a Cognito-trigger Lambda referencing its own pool's ID

Specific to any future Cognito trigger, not just this one: `user_pool.add_trigger(...)` makes
the **pool** depend on the **function**. That same function must never reference
`user_pool.user_pool_id`/`user_pool_arn` in its own environment or IAM policy — that would make
the function depend back on the pool, a cycle. `OncaPreTokenGenFn`'s only environment variable
is the federated-map table name, and its only IAM grant is read access to that same table —
verified in the synthesized template, no user-pool ARN appears anywhere in its role policy.
Cognito trigger events always carry `userPoolId` in the payload if it's ever needed.

## One-time manual setup (not scriptable — needs a human in Google Cloud Console)

This part cannot be done from this codebase or CI — it requires a Google Cloud project and a
human with access to it:

1. In Google Cloud Console, create (or reuse an existing) OAuth 2.0 Client ID
   (Web application type).
2. Add this exact redirect URI (Cognito's federated-IDP callback path — output as
   `GoogleOAuthRedirectUri` once deployed with Google wired, or compute it directly):
   `https://onca-<account-id>.auth.us-east-1.amazoncognito.com/oauth2/idpresponse`
3. Store the resulting client ID + secret in Secrets Manager:
   ```bash
   aws secretsmanager create-secret \
     --name signalscompetitor/onca/google-oauth \
     --secret-string '{"client_id":"<id>.apps.googleusercontent.com","client_secret":"<secret>"}' \
     --profile my2027
   ```
4. Deploy with the client ID as CDK context (the secret is read from Secrets Manager, never
   from context/cdk.json):
   ```bash
   cd infra && cdk deploy OncaPrototypeStack \
     -c google_client_id=<id>.apps.googleusercontent.com --profile my2027
   ```
5. Onboard a design partner for Google login (in addition to, or instead of, a password login):
   ```bash
   python scripts/provision_tenant.py put acme-consorcio entry consorcio betting \
     --google-email ops@acme.example --profile my2027
   ```

## Known gaps (not bugs, just not done)

- **No operator notification on self-registration.** `POST /api/register` creates a real
  `entry`-tier tenant with no alert to anyone — the only way to see who self-registered today
  is `provision_tenant.py list` / scanning `onca-tenant-config` for `entry-<local>-<hex>`
  tenant_ids. A future increment could post to Slack/email on each self-registration.

- **No cross-login-method joining.** A person with both a password account and a Google-mapped
  email for the same tenant gets two separate Cognito users (a real limitation Cognito itself
  has, same as Bluefin's cross-provider gap) — both work independently, neither is aware of the
  other.
- **No revocation helper.** Removing a `OncaFederatedTenantMapTable` row stops a FUTURE token
  from carrying `custom:tenant` (fails closed via the existing 403 path) but does not invalidate
  an already-issued token still inside its TTL — same caveat as revoking any Cognito
  entitlement today, not new to Google login.

## Quick diagnostic commands

```bash
# Does a given email have a resolved tenant mapping?
aws dynamodb get-item \
  --table-name <OncaFederatedTenantMapTable physical name — from stack outputs/resources> \
  --key '{"email":{"S":"ops@acme.example"}}'

# Tail the trigger's own decision-making
aws logs tail /aws/lambda/<OncaPreTokenGenFn physical name> --since 1h

# Re-resolve current resource names if the above 404s
aws dynamodb list-tables --query "TableNames[?starts_with(@, 'OncaPrototypeStack-OncaFederatedTenantMapTable')]"
aws lambda list-functions --query "Functions[?starts_with(FunctionName, 'OncaPrototypeStack-OncaPreTokenGenFn')].FunctionName"
```
