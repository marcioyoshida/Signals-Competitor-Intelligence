# Cognito pool V2 — mutable email (#262)

**Status (2026-10-28): P0–P4 done; P5 waits on the owner.** V2 (`us-east-1_rxEkYhYHg`, email
mutable) has been live since the 2026-10-13 cutover, and every consumer points to it. Fleet
Monitor, the last user of the old pool, moved on 2026-10-25 (Signals-Fleet-Monitor#17). CloudWatch
shows no sign-in, federation or token-refresh datapoints on the old pool since 2026-10-25 04:00Z.
**Left (owner):** a returning Google and Apple sign-in on the phone, then explicit confirmation
to remove the pool. **Left (engineering, after that):** remove the old `build_identity_pool`
block and deploy. The pool is `RemovalPolicy.RETAIN`, so it then needs a manual
`delete-user-pool`.

**Why.** Pool `us-east-1_LfGQGWq56` has `email` required + immutable, and Google/Apple map
email→email. Reproduced on throwaway pools (2026-10-12): the first federated sign-in creates the
user; every later one fails with `user.email: Attribute cannot be updated`, even with an identical
email. Cognito rejects the in-place workarounds (a required attribute must stay in the client's
WriteAttributes and in the IdP mapping). Only a new pool fixes it.

## Phases

| Phase | What | User impact |
|---|---|---|
| P0 ✅ 7d164fd | email→tenant map + self-registration only for provider-verified federated identities (a mutable email would otherwise let a password user claim a mapped tenant) | none |
| P1 ✅ defbc15, LIVE (`us-east-1_rxEkYhYHg`) | CDK: identity block becomes a helper (`infra/identity.py`); new pool `…V2` side by side (email mutable, deletion protection, RETAIN), same clients/IdPs/groups/CDK users/pretoken. `cdk diff` must show additions only. `scripts/migrate_cognito_pool.py` copies the non-CDK native users (owner, directory-reviewer) + group memberships; passwords from where they're stored | none |
| P2 ✅ 4b8a3c8, deployed 2026-10-13 | Deploy B: remove `OncaHostedUi` + `OncaAuthCustomDomain` from the old pool (domain names are global) | **sign-in down** until P3 |
| P3 ✅ a8d7394, deployed 2026-10-13 | Deploy C: both domains on V2; JWT authorizer, session broker, OAuth facade, act API and the site's client id point to V2; `auth.onssa.org` CNAME → new target | everyone signs in once |
| P4 ✅ (audit 2026-10-25: no Lambda env, String SSM parameter or secret references the old pool; Fleet Monitor moved via Signals-Fleet-Monitor#17) | SSM `/fleet-monitor/authtest/onca-*`, QA secret, runbooks | none |
| P5 ⏳ waiting on the owner's Google/Apple check and explicit go-ahead | after 2 stable weeks: remove the old block, delete the old pool by hand | none |

Downtime P2→P3: ~15–60 min (Cognito custom-domain CloudFront + DNS). Rollback before P3 = revert
B; after P3 = remove V2 domains, revert C. The old pool is never modified, so rollback restores
today's state.

## P1 notes
- `cdk diff` (2026-10-12): +32 resources (pool, 2 clients, 2 IdPs, 18 groups, 7 users, 1 group
  attachment, 1 pretoken Lambda permission) + 3 outputs; every existing resource's template is
  byte-identical (only the usual Lambda-asset / BucketDeployment hash noise vs live). Stack at
  415/500 resources until P5 removes the old block.
- After the V2 deploy: `python scripts/migrate_cognito_pool.py --from-pool us-east-1_LfGQGWq56
  --to-pool <OncaUserPoolV2Id> --profile my2027` (dry run), then `--apply`. Creates the owner +
  directory-reviewer, copies groups, sets stored passwords (directory-reviewer, QA, fleet).
  The owner gets `--invite <email>`: created WITHOUT MessageAction=SUPPRESS, so Cognito emails a
  temporary password (sender no-reply@verificationemail.com — check spam) and the owner sets their
  own at first sign-in; re-running with `--invite` re-sends while still FORCE_CHANGE_PASSWORD.
  (`--prompt-password <email>` is the terminal alternative; Cognito refuses "forgot password"
  while a user is FORCE_CHANGE_PASSWORD.)

## Cutover notes (B, C)
- B removes only the two `UserPoolDomain`s; the session broker / OAuth facade env and the
  `GoogleOAuthRedirectUri` / `UserPoolHostedUi` outputs become the same strings as literals,
  `AuthCustomDomainTarget` is dropped. The ACM cert is imported by ARN and DNS is Cloudflare
  (out-of-band): no CFN resource for either.
- Between B and C, delete (or park) the `auth.onssa.org` CNAME so it does not point at the old,
  deleted Cognito CloudFront distribution while C creates the new one.
- C: domains on V2 (same names, same cert), every consumer on V2, site client id
  `4docsvvsocsujjqtj3aofduio3`; new outputs `LegacyUserPoolId`, `AuthCustomDomainTarget` (NEW
  value → the CNAME). The original pool keeps only its pretoken trigger until P5.

## Owner actions
- Lower the Cloudflare TTL on `auth.onssa.org` to 60s before P2; approve the CNAME change in P3.
- Pick the maintenance window; silence the fleet monitor for it.
- Set your own password on V2 from the invitation email (`--invite`).
- No Google/Apple console change: the same domains move, so the `idpresponse` URLs are unchanged.

## What changes for people
- Everyone signs in once after P3 (refresh cookies and ID tokens are pool-bound).
- New `sub` for everyone: MCP clients re-authorize (43 old OAuth token families); push/digest
  rows keyed by sub are empty today.
- Old app builds keep working (prefix domain moves too).

## Verification after P3
1. Google: sign in, out, in again — no `Attribute cannot be updated`; tenant from the map; `/api/feed` 200.
2. Apple: same, on web and in the app (`onca://auth`).
3. Password users: owner has all 17 industry groups; qa-admin in `operator`; fleet health passes.
4. QA Playwright smoke; `/api/session/refresh` cookie round-trip.
5. MCP: authorize → consent → `/mcp` (`scripts/oauth_mcp_e2e.py`); directory-reviewer login.
6. A pre-bridge-2 app build signs in through the prefix domain.
7. A password user who changes their email to a mapped address gets no tenant claim (P0 guard).
