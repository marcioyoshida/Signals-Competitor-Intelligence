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
- Lambda container image, CDK infra (#127) and the rest of the test pillars (#128–#133)
  are not built yet.
