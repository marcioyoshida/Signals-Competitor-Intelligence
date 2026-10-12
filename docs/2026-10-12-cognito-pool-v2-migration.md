# Cognito pool V2 — mutable email (#262)

**Why.** Pool `us-east-1_LfGQGWq56` has `email` required + immutable, and Google/Apple map
email→email. Reproduced on throwaway pools (2026-10-12): the first federated sign-in creates the
user; every later one fails with `user.email: Attribute cannot be updated`, even with an identical
email. Cognito rejects the in-place workarounds (a required attribute must stay in the client's
WriteAttributes and in the IdP mapping). Only a new pool fixes it.

## Phases

| Phase | What | User impact |
|---|---|---|
| P0 ✅ 7d164fd | email→tenant map + self-registration only for provider-verified federated identities (a mutable email would otherwise let a password user claim a mapped tenant) | none |
| P1 | CDK: identity block becomes a helper; new pool `…V2` side by side (email mutable, deletion protection, RETAIN), same clients/IdPs/groups/CDK users/pretoken. `cdk diff` must show additions only. `scripts/migrate_cognito_pool.py` copies the non-CDK native users (owner, directory-reviewer) + group memberships; passwords from where they're stored | none |
| P2 | Deploy B: remove `OncaHostedUi` + `OncaAuthCustomDomain` from the old pool (domain names are global) | **sign-in down** until P3 |
| P3 | Deploy C: both domains on V2; JWT authorizer, session broker, OAuth facade, act API and the site's client id point to V2; `auth.onssa.org` CNAME → new target | everyone signs in once |
| P4 | SSM `/fleet-monitor/authtest/onca-*`, QA secret, runbooks | none |
| P5 | after 2 stable weeks: remove the old block, delete the old pool by hand | none |

Downtime P2→P3: ~15–60 min (Cognito custom-domain CloudFront + DNS). Rollback before P3 = revert
B; after P3 = remove V2 domains, revert C. The old pool is never modified, so rollback restores
today's state.

## Owner actions
- Lower the Cloudflare TTL on `auth.onssa.org` to 60s before P2; approve the CNAME change in P3.
- Pick the maintenance window; silence the fleet monitor for it.
- Set your own password on V2 (or "Esqueci a senha").
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
