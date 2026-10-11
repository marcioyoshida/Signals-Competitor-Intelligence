# Store app — real-device checklist (#259, extends #200)

Run on **a real iPhone (TestFlight)** and **a mid-range Android (internal APK / Play internal track)**
before any store submission, and after any change to `mobile/apps/onca` or the app bridge in
`src/dashboard/site/v2/context.js`. CI (`.github/workflows/mobile-qa.yml`) covers the Android
emulator; this list is what an emulator can't prove.

Use a `qa-internal-*` persona (excluded from `mobile_usage`, so this never trips the M4 gate).

| # | Check | iPhone | Android |
|---|---|---|---|
| 1 | Cold start shows the sign-in gate; no "Assinar", price or storefront link anywhere (#258) | ☐ | ☐ |
| 2 | Sign in with **password** — runs in the system browser, returns to the app signed in (#254) | ☐ | ☐ |
| 3 | Sign in with **Google** — same; never an embedded-WebView Google page | ☐ | ☐ |
| 4 | Sign in with **Apple** (after #248) — same | ☐ | ☐ |
| 5 | Kill the app, reopen after >1 h — still signed in (refresh cookie in the WebView jar) | ☐ | ☐ |
| 6 | Airplane mode → relaunch → "Sem conexão … offline" with **this** user's last feed (#162; iOS needs App-Bound Domains) | ☐ | ☐ |
| 7 | Hoje → row → drawer opens; Esc/back closes; Android back button walks history | ☐ | ☐ |
| 8 | Compartilhar → **native share sheet** with `onssa.org/exec?c=…` only (#258) | ☐ | ☐ |
| 9 | Open a shared `onssa.org/exec?c=…` link from another app → opens in the app (after universal links, #255) | ☐ | ☐ |
| 10 | A source link (e.g. bcb.gov.br) opens in the **system browser**, not inside the app | ☐ | ☐ |
| 11 | Alertas no celular → Ativar → OS permission prompt → subscribed (#256) | ☐ | ☐ |
| 12 | A push arrives with **no content** ("Novo alerta no seu painel Onça" + count); tap opens Hoje › alertas | ☐ | ☐ |
| 13 | Bloqueio do app → Ativar (asks Face ID / digital / senha) → background > N min → locked on return (#257) | ☐ | ☐ |
| 14 | With the lock on: app-switcher thumbnail is the neutral cover; screenshots blocked (Android) | ☐ | ☐ |
| 15 | Sair → back at the gate; push subscription for this device removed; a second user sees nothing of the first | ☐ | ☐ |
| 16 | 390-wide layout: no horizontal overflow; tap targets ≥ 44 px (#245) | ☐ | ☐ |

Record device, OS version, build number and date with the results on the PR or issue.
