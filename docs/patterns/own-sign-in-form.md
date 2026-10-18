# Pattern: the product's own sign-in form (no Cognito managed-login page)

**Status:** live in Anteater (a888626, 2026-10-14) and Onça (#277, 1c1cbc1, 2026-10-18). Fleet
pattern: every product whose clients sign in with an e-mail and password through Cognito should
use it.

## Why

- **Touch targets.** Cognito managed login draws 32px buttons and inputs, a 16px checkbox and a
  19px "Esqueceu sua senha?" link on a phone. Branding can't fix it: `spacingDensity` only accepts
  `REGULAR` and there is no size property (Onça nav-test #277).
- **Custom domains.** The AWS account has used all 4 Cognito custom domains (outage.me, onssa.org,
  mediaframe.me, bluefin-app.com). A new product's sign-in page would otherwise sit on
  `*.auth.us-east-1.amazoncognito.com`.
- **One look, one language.** The form uses the product's own tokens, light and dark, and its own
  copy (pt-BR or en). No second page renders twice, and no locale is forced through `lang=`.
- **No round-trip.** A password sign-in reloads the page in place, so the deep link survives with
  no redirect through the auth domain.

## What the user gets

A sheet over the current page with four steps:

| step | fields | server route |
|---|---|---|
| Entrar | e-mail, senha (show/hide), "Esqueceu a senha?" | `POST /api/session/password` |
| Primeiro acesso (invited user) | nova senha | `POST /api/session/new-password` |
| Esqueceu a senha | e-mail | `POST /api/session/forgot` (always answers ok) |
| Redefinir | código, nova senha, "Enviar outro código" | `POST /api/session/reset` |

Google and Apple stay as buttons under an "ou" divider, and they go **straight to the provider**:
`/oauth2/authorize?identity_provider=Google|SignInWithApple` with PKCE. Cognito still brokers
them, but no Cognito page is shown. In a store app (WebView), those two hand off to the system
browser as before. Google refuses embedded WebViews.

## Server: the session broker

This builds on a session broker that already exchanges the OAuth code server-side and keeps the
refresh token in an HttpOnly cookie (Onça #161/#164). The password routes add:

- `password`: `AdminInitiateAuth` with `ADMIN_USER_PASSWORD_AUTH` (pool id and client id from
  env). It returns the same reply as the code exchange: the ID token in the body, the refresh
  token in the HttpOnly cookie. A `NEW_PASSWORD_REQUIRED` challenge returns
  `{challenge, session}`. Any other challenge returns 409.
- `new-password`: `AdminRespondToAuthChallenge` with the session.
- `forgot`: `ForgotPassword`, always `{"ok": true}`. This keeps the user-enumeration protection
  that `PreventUserExistenceErrors` gives. Only rate limiting surfaces, as a 429.
- `reset`: `ConfirmForgotPassword`. A bad or expired code returns `bad_code`.
- Error mapping, with no Cognito text passed through:
  - 400 `weak_password` / `bad_code`
  - 401 `invalid_credentials` (wrong password, unknown user, disabled user: one message)
  - 429 `too_many_attempts`
  - 502 `unavailable`
- **CSRF:** every body route requires the custom header (Onça: `x-onca-session: 1`). A
  cross-site form can't set it, so it gets a 403.
- Never log the body. The password and code live only in the request.

### Infra

- App client: add `ALLOW_ADMIN_USER_PASSWORD_AUTH` to the explicit auth flows, and keep refresh.
  The pool stays admin-create-only, with the password policy unchanged.
- The broker Lambda gets `cognito-idp:AdminInitiateAuth` and `AdminRespondToAuthChallenge` on
  **that pool's ARN only** (`user_pool.grant(fn, ...)`), plus the `*_USER_POOL_ID` env.
  `ForgotPassword` and `ConfirmForgotPassword` are public client APIs and need no grant.
- HTTP API: the four new `POST` routes on the same integration as the exchange. They need no JWT
  authorizer, since the caller isn't signed in yet. Check the CloudFront behavior that routes
  `/api/session/*` already exists (see the inert-feature traps).

## Client

- `login()` with no provider opens the sheet. `login("Google")` and `login("SignInWithApple")`
  keep going straight to the provider.
- One shared `acceptSession()` handles both the code exchange and the password replies, so the
  token storage and refresh scheduling are the same.
- Controls are **≥44px** (inputs and buttons 48), with **16px** input text so iOS doesn't zoom.
  Use `autocomplete` `username` / `current-password` / `new-password` / `one-time-code`,
  `inputmode`, and `enterkeyhint`.
- Accessibility: `role="dialog" aria-modal`, a focus trap, Esc closes and returns focus to the
  button that opened it. Errors use an icon plus text with `role="alert"`, never colour alone.
- The password is never written to storage. Disable the submit button while a request is in
  flight, and validate the minimum length client-side before calling `new-password` or `reset`.
- A privacy line with a link under the form.

## QA

- The nightly login helper fills the product's form (Onça: `#si-email`, `#si-pass`,
  `#si-submit`) and asserts there is **no request to the auth domain** on the password path.
  Rebuild the QA runner image after changing it.
- Scripts that test the OAuth facade or MCP sign-in keep driving managed login on purpose. That
  page still exists for OAuth clients.
- Live checks:
  - a QA persona signs in on desktop and on a 390px touch viewport;
  - a wrong password shows the error and stores no token;
  - Google/Apple URLs carry `identity_provider=`;
  - the routes refuse requests without the CSRF header;
  - `forgot` for an unknown address returns 200;
  - no control in the sheet is under 44px.

## What stays on managed login

- Every page should use the same form, operator and legacy pages included. In Onça, `/v2/admin`
  (d12a6d6) and the legacy v1 board (32f48b6) moved to it on 2026-10-18. No page builds the
  managed-login URL now, and a test keeps it that way.
- A page without the product's design tokens (Onça v1) can still use the form. The sheet's CSS
  lives in its own file (`v2/signin.css`), which `context.js` attaches itself.
  `<html class="si-standalone">` supplies the tokens scoped to the sheet, and a test keeps them
  equal to `app.css`. If a public page (Onça `/exec`) loads the form, add the stylesheet to the
  edge exemption, to the service-worker shell, and to the deploy invalidation paths.
- OAuth clients (MCP, the agent OAuth facade) use the authorize endpoint and see managed login by
  design.

## Files (Onça)

- `src/dashboard/session_api.py`: `password`, `new_password`, `forgot`, `reset`, `_BODY_ROUTES`
- `infra/app.py`: session Lambda env `ONCA_USER_POOL_ID`, the grant, and the four routes
- `src/dashboard/site/v2/context.js`: `openSignIn()` (the "OWN SIGN-IN FORM" block) and
  `acceptSession()`
- `src/dashboard/site/v2/signin.css`: the `.si*` block (tokens only) and the standalone token block
- `qa_pipeline/lib/auth.py`: `login_with_password`
- `tests/test_session_api.py`, `tests/test_signin_form.py`, `tests/test_managed_login.py`
