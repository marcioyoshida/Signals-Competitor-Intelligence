# Store privacy inventory — Onça app (#252)

**What this is:** the App Store privacy label and the Google Play Data safety answers for the
Onça store app (`mobile/apps/onca`: an Expo shell that hosts `/exec` in a WebView, plus native
push and an optional biometric lock). Both stores count data collected through a web view inside
the app, so everything `/exec` and its APIs collect from a signed-in user is in scope.

**Sources:** `src/dashboard/site/docs/privacy.html` (effective 2026-10-20) and the code below.
Live checks were made 2026-10-20: DynamoDB TTL settings, every Onça log group at 90 days, the
CloudFront log prefix `cf-access/` and contact mail `contato/` at 365 days.

**Keep in sync:** a change to what the app or `/exec` collects must update this file,
`privacy.html` and both store forms together.

## 1. Inventory

The 2026-10-20 privacy policy covers every item in this table. "Linked" means stored with the
user's Cognito `sub` or e-mail. **None of it is used for tracking**, and **none is shared**: AWS,
Stripe, Google, Apple and Expo receive data only as processors acting for Onça.

| Data | Where (code) | Linked | Purpose | Retention | Deleted by "Excluir minha conta" |
|---|---|---|---|---|---|
| **E-mail.** May be an Apple relay `…@privaterelay.appleid.com`, used as given. | Cognito `email`; federated map `<email>`; `sub#<sub>.contact_email` (`invites.py`); digest opt-in `DIGEST#<sub>.email` (`digest_optin.py`); API-key `created_by` (`api_keys_api.py:73`) | Yes | Sign-in, organization mapping, weekly brief and alerts by e-mail | While the account exists | Yes. API-key `created_by` is replaced with `deleted-user:<hash>`; the key stays with the organization (#252). |
| **User ID.** Cognito `sub` and the provider id (`identities`). | Cognito; every per-user row | Yes | Account, entitlement, per-user rows | While the account exists | Yes |
| **No name.** | Google/Apple map only `email` and `email_verified` (`infra/identity.py:132-165`) | — | — | — | — |
| **Organization and groups.** `custom:tenant`, `custom:tier`, groups. | Cognito; `OncaTenantConfig` (organization row, no user e-mail) | Yes | Entitlement | Contract | User attributes yes; the organization row stays (a sole-user Entry tenant is closed) |
| **Device push token.** `expo:ExponentPushToken[…]`; web push endpoint on the web. | `OncaPushTable` `SUB#<hash>`, with sub, tenant, prefs and the ids of events already alerted (`push.py`) | Yes | Content-free alert notifications. Opt-in. | Until unsubscribed, the token is rejected (404/410 or 3 failures), or entitlement is lost | Yes |
| **Product interaction (engagement).** Kind/action, card, officer, sector, entity, device **class** only. | `ENGAGEMENT#` rows (`src/synth/engagement_log.py:85-101`) | Yes (actor = sub) | Analytics: aggregate operator rollups (§E, `mobile_usage` #165). No per-user personalization, no ads. | While the account exists | Yes |
| **Decisions.** Verdict, optional free-text rationale, outcome note, device class. | `DECISION#` rows (`decision_log.py`); `/api/act` journal `OncaCurationLog` (args include rationale) | Yes | App functionality: the organization's decision log | Contract (the organization is the controller) | Yes, with their journal rows |
| **Defect/suggestion reports ("Reportar").** Text ≤1000 chars, plus that turn's question and answer and the screen context. | `OncaFeedback` (`feedback.py:98-109`) | Yes (sub) | Customer support / fixing defects. Never published. | 90 days (TTL) | Yes |
| **Questions to Ask Onça.** | Sent to Amazon Bedrock to answer, not stored with identity. The transcript stays in the tab's `sessionStorage`. `OncaAskQuota` daily counter (TTL 3 days). | Counter only | App functionality | Counter 3 days | Counter yes |
| **Unanswered questions.** First turn only. | S3 `coverage_gaps/index.json`: text and counts, **no identity** (`src/synth/coverage.py`) | **No** | Fixing coverage gaps. May become a public issue. | 12 months after last asked | Not applicable (not linked) |
| **Connected AI apps (MCP/A2A).** Which app, when, last use; hashed tokens. | `OncaOAuth` (TTL) | Yes | App functionality | Token TTLs (1 h / 30 d) | Revoked |
| **Site access logs.** IP, URL with query string, time, user agent (the app adds `OncaApp/0.1`), referrer. Includes the app's sign-in beacon `?app_auth=` (e-mails masked). | CloudFront standard logs, `cf-access/` | No: no user id, and IP only | Security; Marco Civil art. 15 obligation | 365 days (lifecycle `privacy-cf-access-365d`) | Expire on their own |
| **Invite abuse counter.** Raw IP. | Federated map `rl#ip#<ip>#<hour>` (`invites.py:115`) | No | Rate-limit invite redemption | 2 hours (TTL) | Expires on its own |
| **Server logs.** Errors and counters. Never question text, tokens, passwords, e-mails or user ids (the one raw-sub log line was removed in #252). | CloudWatch | No | Diagnostics | 90 days | Expire on their own |
| **Billing.** Plan, modules, status, Stripe event ids. **No card data.** **The app has no purchase path.** CTAs are hidden in app mode (#258), and purchase URLs are blocked even in the system browser. | `OncaTenantConfig.subs`, `BILLING#` rows | Organization, not user | Billing; tax law | Tax-law period | Stays with the organization |
| **Contact e-mails.** | S3 `contato/`, sent from the user's own mail app, not from the app | — | Support | 365 days | On request |
| **On-device only, never sent.** Offline copy of the last dashboard and session markers (wiped on sign-out); app-lock preference in SecureStore. **Face ID/fingerprint never leaves the OS** (`expo-local-authentication`). | Device | — | — | — | — |

**Backups:** point-in-time recovery (35 days) is on for the entities, curation-log,
federated-map, tenant and API-key tables. Deleted rows age out of the backups within 35 days.
Backups are never used to restore a deleted account (stated in the policy and on the deletion page).

**Not collected:** location (precise or coarse; IP is never mapped to a location), contacts,
photos and files, health, financial account or card data, browsing history, advertising ID,
crash reports, audio, messages.

## 2. Tracking: **No**. No ATT prompt.

Verified 2026-10-20:
- **App dependencies** (`mobile/apps/onca/package.json` and the lockfile): expo, expo-constants,
  expo-local-authentication, expo-notifications, expo-screen-capture, expo-secure-store,
  expo-web-browser, react, react-native, react-native-safe-area-context, react-native-webview.
  - None is an analytics, ads, attribution or crash SDK: no Firebase, Sentry, Bugsnag,
    Crashlytics, expo-insights, expo-updates telemetry, AdSupport or AppTrackingTransparency.
- **Web app:** only same-origin scripts and styles (`/v2/app.js`, `/v2/context.js`,
  `/v2/app.css`), with system fonts.
  - No third-party script, font, tag manager or CDN.
  - External hosts appear only as links: `auth.onssa.org` (Cognito sign-in), `signals-llc.store`
    (web-only purchase, unreachable in the app), and GitHub, App Store and YouTube on admin pages.
- **No data is combined with third-party data for advertising or sold to data brokers.**
- **Re-check before each store submission:**
  ```bash
  grep -Ei "firebase|sentry|bugsnag|crashlytics|insights|adsupport|tracking|analytics|segment|amplitude|mixpanel" mobile/apps/onca/package-lock.json
  ```
  It must print nothing. Also, no `<script src="http` in `src/dashboard/site/v3/` or `v2/`.

## 3. App Store Connect → App Privacy (ready to paste)

**Privacy policy URL:** `https://onssa.org/docs/privacy.html`
**Do you or your third-party partners collect data from this app?** Yes.

For every type below: **Used for tracking: No.**

| Category → Type | Linked to the user | Purposes to tick |
|---|---|---|
| Contact Info → **Email Address** | Yes | App Functionality |
| Identifiers → **User ID** | Yes | App Functionality |
| Identifiers → **Device ID** (the push token; declared conservatively) | Yes | App Functionality |
| Usage Data → **Product Interaction** | Yes | Analytics, App Functionality |
| User Content → **Customer Support** ("Reportar" reports) | Yes | App Functionality |
| User Content → **Other User Content** (decision rationale and outcome notes) | Yes | App Functionality |
| User Content → **Other User Content**, second entry *if* Apple lets the same type carry both a linked and a not-linked declaration; otherwise keep only the linked one above (unanswered questions, stored without identity) | No | App Functionality |
| Diagnostics → **Other Diagnostic Data** (site access logs) | No | App Functionality |

Answer **Not collected** for:
- Health & Fitness, Financial Info, Location, Sensitive Info, Contacts, Browsing History and
  Search History;
- Emails or Text Messages, Photos or Videos, Audio, Gameplay Content and Purchases;
- Crash Data, Performance Data, Name, Phone, Physical Address and Other Contact Info.

Notes for the reviewer field, if asked:
- Purchases happen only on the website; the app has no purchase path (Guideline 3.1.1).
- Face ID stays on the device.
- Account deletion is in the app at *Sua conta → Excluir minha conta*, and on the web at
  `https://onssa.org/docs/excluir-conta.html` (Guideline 5.1.1(v)).

## 4. Google Play Console → Data safety (ready to paste)

**Overview**

| Question | Answer |
|---|---|
| Does your app collect or share any of the required user data types? | **Yes** |
| Is all of the user data collected by your app encrypted in transit? | **Yes** (HTTPS only; the WebView loads `https://onssa.org` and the push relays use TLS) |
| Do you provide a way for users to request that their data is deleted? | **Yes**: in the app, and at `https://onssa.org/docs/excluir-conta.html` (also the *Delete account URL* field) |
| Shared with third parties? | **No** for every type. Service providers acting for us are not "sharing" under Play's definition. |

**Data types collected**

| Play data type | Collected | Optional? | Processed ephemerally? | Purposes |
|---|---|---|---|---|
| Personal info → **Email address** | Yes | Required | No | App functionality, Account management |
| Personal info → **User IDs** | Yes | Required | No | App functionality, Account management |
| App activity → **App interactions** | Yes | Required (automatic) | No | Analytics, App functionality |
| App activity → **Other user-generated content** (decision notes, reports) | Yes | Optional | No | App functionality |
| App activity → **In-app search history** | No. Ask questions are processed to answer, not stored with identity. | — | — | — |
| App info and performance → **Diagnostics** (access logs) | Yes | Required | No | App functionality, Fraud prevention, security and compliance |
| Device or other IDs (push token) | Yes | Optional (only when alerts are on) | No | App functionality |

Everything else is **not collected**:
- Name, Address, Phone, Race and ethnicity, Political or religious beliefs, Sexual orientation,
  Other info;
- Financial info (all), Health and fitness, Messages, Photos and videos, Audio, Files and docs,
  Calendar, Contacts, Location (approximate and precise);
- Web browsing, Installed apps, Crash logs.

## 5. Owner steps

1. **App Store Connect:** open *Onça → App Privacy*, enter section 3 and publish. Set the privacy
   policy URL.
2. **Play Console:** open *App content → Data safety*, enter section 4, and set the *Delete account URL*.
3. Re-run the tracking check in section 2 against the build you submit.

## 6. Known caveats (not blockers)

- **Retention is enforced by a script, not CDK:** the 90-day log retention and the 365-day
  CloudFront/contact lifecycles are set by `scripts/privacy_retention.sh`. Re-run it after
  deploying a new Lambda; a new log group starts with no expiry.
- **Push subscriptions have no TTL:** they rely on the cleanup rules in section 1. The push table's TTL
  (`expires_at`, enabled in #252) sweeps only the digest unsubscribe tokens (`DIGESTTOK#`, +400 days).
- Two empty, orphaned contact-mail buckets (`…contactmailbucket…-j3anwi05tvue` and
  `…-ppbiwsc7fk1f`, no lifecycle) were deleted 2026-10-23. The only contact-mail bucket is the
  stack's `…-ny4niagkiuzf`.
