# ADR 029 — Onça in the App Store and Google Play: an Expo shell around the PWA, Sign in with Apple, and store compliance

- Status: **Proposed** — 2026-10-11. Owner-requested: *"Fill the ADR for mobile, raise the stories
  for it. Apple auth and so."*
- Builds on epic **#172** (Onça on the phone, PWA-first): M0 + M1 shipped (#161–#170), and the
  real-device pass is #200. The store app was a single gated story, **#171**.
- Revisits **#171's Capacitor choice**. Precedent is fleet **ADR 0023** (Bluefin,
  `Signals-Creator-Radar/docs/adr/0023-react-native-mobile-apps.md`), which said "React Native for
  Bluefin/Tyto; **Capacitor wrap or nothing** for the BR analyst forks".
- Does **not** propose a native rewrite. The UI stays the `/exec` PWA. What changes is the *wrapper
  technology* and the work the stores require around it.

## Context

### What already exists (verified 2026-10-11)

| Piece | Where | State |
| --- | --- | --- |
| Officer suite on the phone | `/exec` (v3), Hoje view, drawer, offline feed, content-free web push | **Live.** Phone QA gate 73/73 in WebKit. 44px touch targets (#245). |
| Session | Cognito Hosted UI + PKCE. Refresh token held only in the `onca_rt` HttpOnly cookie (`/api/session`, `session_api.py`). | Live. Callback/logout URLs registered: `/` and `/exec` only (#242 routes other screens through `/exec`). |
| Federated login | Google IdP on the pool. An operator maps a corporate email to a tenant (`tenant_config.map_federated_email`); an unmapped Google login may self-register an **Entry** tenant (`/api/register`). | Live. |
| Push | VAPID web push, payload-free (`webpush.py`), `OncaPushNotifier` hourly 10–23 UTC. | Live. **No APNs/FCM.** |
| Account deletion | Privacy page says "write to contato@onssa.org; deleted within 30 days". | **Manual only.** |
| Fleet mobile stack | `Signals-Creator-Radar/mobile`: `@signals/mobile-shell` (ForkConfig, system-browser PKCE with `<scheme>://auth` and `identity_provider`, API client, device registration, purchase-route contract, view states) + the Expo app `apps/bluefin`. | Live for Bluefin: Expo SDK 57, EAS builds, iOS push works, TestFlight build 21. The shell was designed for a second fork (SCR #97) and is boundary-tested against Bluefin nouns. |
| Apple developer account | Team `AWSP6DBQ68`. | **Individual** ("Pessoa física"). Sign in with Apple is live for Bluefin (Services ID `com.bluefin-app.signin`). Conversion to an organization waits on the entity rename (Signals, LLC → Smart Signals LLC) and a D-U-N-S number. |

### Why #171's Capacitor plan doesn't survive contact with this environment

1. **iOS can't be built here.** This is WSL on Windows with no Mac. ADR 0023's decisive finding was
   that Expo **EAS Build** solves this (hosted macOS, free tier 15 iOS + 15 Android builds a month).
   Capacitor has no first-party equivalent. The options are Ionic Appflow (paid), Codemagic, or GitHub
   macOS runners (10× minute multiplier on a private repo). Each is a second build pipeline the fleet
   doesn't have.
2. **The fleet already paid for the hard parts once.** That covers push registration, PKCE in the
   system browser, device registration, crash reporting, the purchase-route contract and the
   TestFlight/EAS release runbooks (`Signals-Creator-Radar/docs/runbooks/`). A Capacitor app re-does
   all of it in a different stack.
3. **"Wrap" was the point, not "Capacitor".** ADR 0023 ruled out a native UI for the analyst forks.
   It didn't rule out a native *shell*. An Expo app whose main screen is a `WebView` of `/exec` is
   still a wrap: one UI codebase, the PWA.

### What the stores will ask for, and Onça doesn't have yet

| Requirement | Rule | Onça today |
| --- | --- | --- |
| Offer Sign in with Apple when Google sign-in is offered | App Store Review Guideline **4.8** | Google only |
| Let users start account deletion **inside the app** (and, for Play, from a web URL) | Guideline **5.1.1(v)**; Play User Data policy | Email request only |
| More than a repackaged website | Guideline **4.2** (minimum functionality) | Push, offline and share exist on the web; nothing native |
| No purchase buttons or links outside IAP (US/BR link-out rules aside) | Guideline **3.1.1 / 3.1.3** | `/exec` shows "Assinar" / upgrade CTAs to the Stripe storefront |
| A working demo account for App Review | App Review notes | QA personas are internal and excluded from usage metrics; not suitable as a reviewer login |
| Privacy nutrition labels; Play Data safety form | App Store Connect / Play Console | Privacy policy exists; no store-format inventory |
| Seller of record | App Store / Play listing | The Apple team is individual, so the listing would show a person, not the company |

### Two traps specific to Onça's identity model

- **Hide My Email breaks operator mapping.** Apple may give Onça
  `xyz@privaterelay.appleid.com` instead of the user's corporate address. `map_federated_email` maps
  a *corporate* email to a tenant, so an invited officer who hides their email doesn't match. Today
  they would land on the Entry **self-registration** form and could create a separate, tiny tenant
  instead of joining their organization. That is a silent mis-provisioning, not just a login error.
- **Relay addresses only receive mail from registered senders.** The weekly brief and alert emails
  go out through SES. Mail to `@privaterelay.appleid.com` is dropped unless the sending domain is
  registered with Apple's Private Email Relay Service and passes SPF/DKIM. Bluefin hit and solved
  this (custom MAIL FROM, SCR #124).

### WebView-specific risks (to settle in a spike, not assume)

- **Embedded-browser OAuth is blocked by Google** (`disallowed_useragent`), and Apple discourages it.
  Sign-in must run in the system browser (ASWebAuthenticationSession / Custom Tabs). The shell already
  does this. The result then has to be handed to the WebView.
- **Service workers in WKWebView** need App-Bound Domains (`WKAppBoundDomains`, iOS 14+). Without
  them `/exec-sw.js` doesn't run, and offline (#162) silently degrades in the app.
- **Cookies.** The `onca_rt` refresh cookie must live in the WebView's cookie store, because that is
  where `/api/session/refresh` runs.

## Decision

1. **Store app = the fleet Expo shell hosting `/exec` in a WebView.** It is a new
   `ForkConfig("onca")` on `@signals/mobile-shell` plus a thin `apps/onca` Expo app. There is no
   native screen beyond the shell's own states (sign-in, offline, error, update). This **supersedes
   #171's Capacitor choice**. If the spike (story 7) finds a blocker the WebView can't clear, the
   fallback is Capacitor plus a hosted macOS builder, decided in that spike and recorded here.
2. **Sign in with Apple ships on the web first, through Cognito.** Add a `SignInWithApple` identity
   provider on the existing pool, with its own Onça App ID, Services ID and key under team
   `AWSP6DBQ68`. Don't group it under Bluefin's App ID, because grouped apps share the user identifier
   and the consent branding. The Hosted UI gets "Continuar com a Apple". The app uses the same IdP
   through the shell's `identity_provider=SignInWithApple`, which satisfies 4.8. It is useful on the
   web regardless of the store.
3. **Federated identity never auto-provisions an invited user.** A sign-in whose email is an Apple
   relay address must not create a new Entry tenant silently. The no-access gate says what happened,
   and the user either (a) signs in with the email their organization registered, or (b) enters an
   **invite code** that an operator issues per user, which links the federated identity to the tenant.
   Self-registration stays available for genuine individual Entry sign-ups, and the gate states
   explicitly that it creates a new account.
4. **Account deletion becomes self-service everywhere.** A "Excluir minha conta" action (in `/exec`'s
   account menu, therefore also in the app) goes through `/api/me/act`. It deletes the Cognito user,
   revokes sessions, and deletes that user's push subscriptions, decisions and engagement within the
   privacy policy's 30 days. Organization data stays with the contract. A public web page gives Play
   its deletion URL. The privacy page is updated to match.
5. **The app is a reader for paid features.** Inside the app, every purchase and upgrade CTA is
   hidden, and the app shows no link to the storefront. A user manages their plan on the web. The
   shell's purchase-route contract is the switch. IAP and the US link-out entitlement are out of scope
   until a customer buys in-app (revisit with the 3.1.3 / link-out economics in ADR 0023).
6. **Native value is deliberate, for 4.2 and for InfoSec buyers.** The app adds:
   - APNs/FCM push under the **same content-free policy** as web push: "Novo alerta no seu painel
     Onça" + a count, never a finding;
   - an optional biometric app lock and a privacy screen in the app switcher;
   - native share sheet for #170 card links;
   - universal links for `onssa.org/exec`.
7. **Gates.**
   - Everything up to an **internal TestFlight / Play internal-testing build** is **ungated**. It
     de-risks the shell and most of it improves the web product anyway.
   - **Public store release** keeps #171's demand gate: at least 1 paying or signed customer requires
     store/MDM distribution in writing, OR at least 30% of officer sessions on phones for 4 consecutive
     weeks (QA/operator excluded, per the 2026-09-29 fix) plus at least 1 app request.
   - Public release also requires the Apple team to be an **organization** (Smart Signals LLC,
     D-U-N-S), so the seller of record is the company.

## Consequences

- **Fleet coupling.** Onça's app depends on `@signals/mobile-shell`, which lives in the **Bluefin**
  repo. Under the tickets-only rule for fork repos, shell changes Onça needs are filed as
  `Signals-Creator-Radar` tickets, not committed from this repo. Two remedies: the planned extraction
  of the shell to its own repo (SCR #113), or vendoring a pinned copy into this repo. Story 8 decides.
- **One EAS free tier for two apps.** The fleet now shares 15 iOS + 15 Android builds a month. That
  is enough for internal builds. If it isn't, EAS Production is a cost decision for the owner.
- **Cognito changes are owner-assisted.** Creating the App ID, Services ID and key in the Apple
  portal needs the owner's interactive 2FA login. Committing their IDs to `infra/cdk.json` context
  (never `-c` flags: the 2026-09-15 Google IdP incident) is Claude's.
- **New callback URL.** The app's `onca://auth` redirect is added to the app client's callback and
  logout URLs. It is not a web URL, so it doesn't widen the web attack surface.
- **The privacy policy, the terms and `/docs/celular-seguranca.html` change**: deletion,
  Apple relay, and app data (device token, crash reports).
- Web users gain Apple sign-in, invite codes and self-service deletion **whether or not the app ever
  ships**.

## Rejected alternatives

- **Capacitor + a hosted macOS builder** (the #171 plan). It's viable, but it's a second mobile
  stack and a second build pipeline, and it duplicates push, auth and release work the fleet already
  has. It stays only as the spike's fallback.
- **React Native screens for Onça.** This is what ADR 0023 rejected for the analyst forks: two UIs
  to keep in sync for a small, senior audience whose product is dense tables and drawers.
- **No app, PWA only.** That remains the default *until the gate*. It is rejected as the end state
  only because some regulated buyers allow only store- or MDM-distributed apps on corporate phones
  (#171's user story).
- **Native Sign in with Apple in-app via `expo-apple-authentication`, exchanging the identity token
  ourselves.** Cognito user pools don't accept a raw Apple identity token for federation, so this
  would mean a custom token-exchange Lambda and a second trust path. The Hosted UI in the system
  browser through the shell meets 4.8 and keeps one path.
- **Grouping Onça under Bluefin's Apple App ID** to reuse its key: it shares user identifiers and
  consent branding across two unrelated products.

## Stories

Raised 2026-10-11 as GitHub issues **#248–#260** under epic **#172**, label `mobile`:
- Milestone **Mobile M2 — Store readiness** covers stories 1–6. They are ungated and also improve
  the web.
- Milestone **Mobile M3 — App shell & internal builds** covers stories 7–12. They are ungated and
  end in internal testing.
- Milestone **Mobile M4 — Store release (gated)** covers story 13.

1. **#248** — Sign in with Apple on the web through Cognito (Apple App ID, Services ID and key; IdP in CDK).
2. **#249** — Hide My Email: invite codes link a federated identity to a tenant; no silent Entry self-registration for invited users.
3. **#250** — Apple Private Email Relay: register the sending domain so digests and alerts reach relay addresses.
4. **#251** — Self-service account deletion (in-app and web), plus a public deletion page for Play.
5. **#252** — Store privacy inventory: App Store nutrition labels and the Play Data safety form.
6. **#253** — Organization enrolment: Apple team individual → organization (D-U-N-S), Google Play organization account. Owner-run and blocked on the entity rename.
7. **#254** — Spike: `/exec` in the fleet Expo shell's WebView. Covers the auth handoff, cookies, App-Bound Domains for the service worker, and offline; a go/no-go decision with the Capacitor fallback.
8. **#255** — Onça ForkConfig + `apps/onca` Expo skeleton (bundle ID, EAS project, icons, `onca://auth`, universal links + AASA); decide shell extraction vs vendoring.
9. **#256** — Native push (APNs/FCM) with device registration on Onça's API; `OncaPushNotifier` sends web push and native push, both content-free.
10. **#257** — App lock (biometric) and app-switcher privacy screen.
11. **#258** — App mode: hide purchase/upgrade CTAs, open external links in the system browser, native share sheet for card links.
12. **#259** — App QA: Android emulator flows in CI, plus a TestFlight/internal-track device checklist extending #200.
13. **#260** — Store release (gated): listings, screenshots, review notes, a reviewer demo tenant, and the 4.2 justification. Supersedes #171.
